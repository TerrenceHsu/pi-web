# P1-E M1 — Multi-Provider Runtime Design

> **状态**：DESIGN FROZEN — APPROVED FOR M1-1
> **基线**：master `c796a01`（M1-0 audit committed @ `3a1e011` pivot）
> **日期**：2026-07-19
> **前置**：[p1-e2-provider-profiles.md §19 Pivot 附录](p1-e2-provider-profiles.md)
> **范围**：M1-0 Provider Contract Audit + 冻结决策 + 修订缺口；M1-1 实施前不再变更

## 0. M1 目标

把 Backend Foundation（Credential + Profile + Binding）接到真实 Prompt / Regenerate 执行：

- **GLM** 包装现有 `GLMProviderAdapter`（Core Runtime diff = 0）
- **Qwen / Kimi** 共用一个新增的 `OpenAICompatibleProvider`
- **ProviderFactory** 是唯一知道「哪个 Provider 用哪个 Adapter」的位置
- **`web/provider_runtime.py`** 负责请求级临时绑定 `harness.agent.client` + finally 还原 + close

请求启动后 provider/model 不可变；运行中切换只影响下次请求；Regenerate 用当前 Session 当前模型。

## 1. Adapter Contract 审计（frozen）

以下接口由 `providers/base.py` / `stream_events.py` / `providers/errors.py` 定义，M1-1 `OpenAICompatibleProvider` **必须**围绕这套 contract 实现——不发明第二套事件模型。

### 1.1 ProviderAdapter（`providers/base.py`）

```python
class ProviderAdapter(abc.ABC):
    provider_id: str = ""   # 类属性 / 实例可覆盖
    model: str = ""         # 类属性 / 实例可覆盖

    @abc.abstractmethod
    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]: ...

    async def aclose(self) -> None: ...   # 默认 no-op；幂等
```

契约要点：
- `stream()` 是 async generator，按顺序 yield `StreamEvent`
- 必须以 `DoneEvent` 或 `ErrorEvent` 收尾（除非抛 `ProviderError` 被 `ModelClient` 捕获后转 `ErrorEvent`）
- `aclose()` 释放底层 SDK client（httpx 连接池）；多次调用不抛错
- `signal.is_set()` 时尽快 yield `DoneEvent(stop_reason="aborted")`（协作式中止）

### 1.2 ProviderRequest（`providers/base.py`，Pydantic BaseModel）

```python
class ProviderRequest(BaseModel):
    system_prompt: str
    messages: list[LLMMessage]
    tools: list[ToolDef] = Field(default_factory=list)
    signal: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

M1-1 直接复用，不扩展。

### 1.3 StreamEvent 5 种（`stream_events.py`）

| 事件 | 字段 | 用途 |
|---|---|---|
| `TextDeltaEvent` | `delta: str` | 文本增量 |
| `ToolCallEvent` | `tool_call: ToolCall`（id / name / arguments / raw） | 工具调用 |
| `DoneEvent` | `stop_reason ∈ {stop, length, tool_use, aborted}` / `usage: Usage` | 正常结束 |
| `ErrorEvent` | `message: str` | 真正错误（aborted 不走此路径） |

### 1.4 ProviderError 层级（`providers/errors.py`）

```
ProviderError
├── ProviderConfigError          # 配置缺失 / 非法（缺 api_key、bad base_url）
├── ProviderProtocolError        # 协议层错（invalid tool input JSON / unknown event）
├── ProviderAuthenticationError  # 401 / 403
├── ProviderRateLimitError       # 429 / quota 用尽
└── ProviderStreamError          # 流式中网络异常 / 连接断
```

`ModelClient.stream` 统一捕获 `ProviderError` + 未知 `Exception` → `ErrorEvent`，不让异常穿透 Agent loop。

### 1.5 ModelClient 是 thin wrapper（`model_client.py`）

```python
class ModelClient:
    provider_id: str
    api_id: str
    model: str
    adapter: ProviderAdapter   # 持有 adapter

    def __init__(self, adapter: ProviderAdapter) -> None: ...
    async def stream(*, system_prompt, messages, tools, signal, metadata) -> AsyncIterator[StreamEvent]: ...
    async def close(self) -> None: ...   # 转发 adapter.aclose()；吞异常
```

## 2. Agent 持有 Provider 的方式

### 2.1 Agent.client 字段（`agent.py:128, 138, 375`）

```python
class Agent:
    def __init__(self, *, client: ModelClient, ...):
        self.client = client
```

`Agent.run_prompt` 通过 `client=self.client` 传给 `loop`——**这是 provider 的唯一持有引用**。

### 2.2 Harness.close() 清理路径（`harness.py:531-556`）

```python
async def close(self) -> None:
    # Step 17: detach MCP
    try: await self.detach_mcp_servers()
    except Exception: pass
    # Step 21: 关 ModelClient（释放 httpx 连接）
    try:
        close_fn = getattr(self.agent.client, "close", None)
        if close_fn is not None:
            await close_fn()
    except Exception: pass
    # 再 unsubscribe
    ...
```

**M1 影响**：`provider_runtime` 临时替换 `harness.agent.client` 后，**原 client 仍由 Harness.close() 负责关闭**；request client（新建的）由 `provider_runtime` 的 finally 关闭。两者职责分离，互不干扰。

### 2.3 临时替换 + finally 还原范式（harness.py `_run_one:391-509` 范例）

Skill 注入已用此模式：

```python
original_system_prompt = self.agent.system_prompt
try:
    self.agent.system_prompt = rendered_prompt   # 临时替换
    try:
        # ... 执行 ...
    finally:
        self.agent.system_prompt = original_system_prompt   # 还原
finally:
    self._snapshot_builder = None
```

**M1-4 `bind_to_harness`** 沿用此模式（详见 §7.7）。

## 3. _execute_prompt 是唯一 bind_to_harness 接入点（修订 A）

### 3.1 `_execute_prompt` 是真正的执行入口

`_run_prompt_core`（`web/app.py:1280-1292`）是 D2-4 thin wrapper：

```python
async def _run_prompt_core(validated: _PromptValidated) -> PromptRunOutcome:
    execution = await _execute_prompt(validated)
    return await _persist_normal_prompt_result(validated, execution)
```

`_run_regeneration_core`（`web/app.py:1334+`）同样通过 `_execute_prompt` 执行 LLM：

```python
async def _run_regeneration_core(validated, *, assistant_message_id, request_id, revision_id=None):
    # ... revision 创建 + canonical 截断 + 临时替换 harness state ...
    execution = await _execute_prompt(
        validated,
        override_initial_messages=regeneration_history,
        suppress_user_append=True,
    )
    # ... finalize_revision + reset ...
```

两条路径都汇聚到 `_execute_prompt`——**这是唯一接入点**。

### 3.2 修订 A 决策：只 wrap 一次

`provider_runtime.bind_to_harness` **只在 `_execute_prompt` 内激活一次**。`_run_regeneration_core` 不再单独 wrap。

理由：
- 避免 nested context manager（regenerate 路径已有 harness state 临时替换 + revision 事务边界，再加一层 wrap 增加复杂度）
- `_execute_prompt` 是 Prompt 和 Regenerate 的公共底层
- Regenerate 仍使用当前 Session Binding（请求启动时 `resolve_selection` 冻结）——符合 §8.5 决策 7

### 3.3 M1-6 仅增加 Regenerate 行为测试

M1-6 **不修改** `_run_regeneration_core` 源码——只加端到端行为测试验证：

- Regenerate 用当前 Session Binding 的 Provider/Model
- Regenerate 后 revision `content_json` 含正确的 `provider` / `model` / `usage`
- Regenerate 失败（如 401）→ revision.status="error"，不影响下个请求
- Regenerate 期间切换 Binding → 当前 regenerate 不变；下一次 regenerate 用新 Binding

### 3.4 事务边界（D2-4 不变量）

D2-4 保证：**短事务 create_running_revision → 释放 → 长 LLM 执行 → finalize 短事务**——没有 SQLite `BEGIN IMMEDIATE` 跨越 LLM 调用。

M1-4 `bind_to_harness` 在 LLM 执行窗口内激活——**不跨 SQLite 事务**，与 D2-4 不变量兼容。

## 4. ProviderDefinition + M1-2 Qwen / Kimi presets

### 4.1 当前 `_DEFAULT_REGISTRY`（`registry.py:239-241`）

```python
_DEFAULT_REGISTRY = ProviderRegistry((_GLM_DEFINITION, _ANTHROPIC_DEFINITION))
```

两者都是 `api_style="anthropic_compatible"`。

### 4.2 `api_style` 已预留 `openai_compatible`

```python
ProviderAPIStyle = Literal["anthropic_compatible", "openai_compatible"]
```

`registry.py:24` 文档注释明确「`openai_compatible` 留待 P1-E2 引入」——E2 没引入（按 Pivot），**M1-2 顺势引入 `qwen` / `kimi`**。

### 4.3 冻结决策 1 / 2：Qwen / Kimi base_url

```python
_QWEN_DEFINITION = ProviderDefinition(
    id="qwen",
    display_name="Alibaba Qwen (OpenAI-compatible)",
    api_style="openai_compatible",
    default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    credential_validation_strategy="unsupported",   # M1 不做远端 Key validation
    credential_validation_endpoint=None,
    supports_model_listing=False,
    key_prefix_hints=("sk-",),   # 弱提示，confidence=unknown
)

_KIMI_DEFINITION = ProviderDefinition(
    id="kimi",
    display_name="Moonshot Kimi (OpenAI-compatible)",
    api_style="openai_compatible",
    default_base_url="https://api.moonshot.cn/v1",
    credential_validation_strategy="unsupported",
    credential_validation_endpoint=None,
    supports_model_listing=False,
    key_prefix_hints=("sk-",),
)

_DEFAULT_REGISTRY = ProviderRegistry(
    (_GLM_DEFINITION, _ANTHROPIC_DEFINITION, _QWEN_DEFINITION, _KIMI_DEFINITION),
)
```

**Qwen 范围明确（决策 1）**：只支持**中国华北 2 北京共享 DashScope endpoint**。海外区域（如新加坡 / 法兰克福）和 Workspace 专属 endpoint **不在 M1 范围**——若用户需要其他区域，需要等 Custom Base URL 单独评估。

**Kimi 范围（决策 2）**：`https://api.moonshot.cn/v1`——Moonshot 官方公开 endpoint。

`base_url` 用户不能改（M1 不支持 Custom Base URL）；用户在 Provider Settings Modal 只能填 API Key + Model ID。

### 4.4 `GET /api/provider-definitions` safe 字段

只返回：
- `id` / `display_name` / `api_style`
- `supports_model_listing`（M1 内仍只返回静态建议）
- `key_prefix_hints`（前端 hint 显示）

**不返回**：
- `credential_validation_endpoint`（内部 endpoint）
- `default_base_url`（避免泄露调用目标；前端只展示 display_name）
- Authorization / SDK 内部信息

## 5. M1-1 `OpenAICompatibleProvider` 设计

### 5.1 模块位置

```
src/pi_agent_core_py/providers/openai_compat.py
```

### 5.2 OpenAICompatConfig（修订 D）

```python
from pydantic import BaseModel, Field, SecretStr

class OpenAICompatConfig(BaseModel):
    api_key: SecretStr = Field(repr=False)   # 不进 repr / log
    base_url: str
    model: str
    timeout_s: float = 60.0
    max_tokens: int | None = 4096            # None = 不传给 SDK
    temperature: float | None = None         # None = 不传给 SDK（默认行为）
```

**删除字段**：`extra_headers`（M1 不暴露给用户；若需固定 header 在 Adapter 内部硬编码）。

**关键变化**：
- `api_key` 改为 `SecretStr`——Pydantic 序列化 / repr 时不暴露明文
- `max_tokens` / `temperature` 允许 `None`——不传给 SDK（让 SDK 用默认）
- 不保存 `provider_id`（决策 3）——由 `OpenAICompatibleProvider` 构造参数传入

### 5.3 OpenAICompatibleProvider 类（决策 3）

```python
class OpenAICompatibleProvider(ProviderAdapter):
    def __init__(
        self,
        config: OpenAICompatConfig,
        *,
        provider_id: str,                       # "qwen" / "kimi"
        client: AsyncOpenAI | None = None,      # 测试注入
    ) -> None:
        if not config.api_key:
            raise ProviderConfigError("OpenAICompatConfig.api_key 不能为空")
        if not config.model:
            raise ProviderConfigError("OpenAICompatConfig.model 不能为空")
        if not provider_id:
            raise ProviderConfigError("provider_id 必须传入")

        self.config = config
        # 实例属性覆盖类属性——区分 Qwen / Kimi
        self.provider_id = provider_id
        self.model = config.model

        self._client = client or AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            timeout=config.timeout_s,
        )
        self._closed: bool = False
```

**Qwen / Kimi 共用 Config 和 Adapter 类**——区别仅在构造参数 `provider_id` 和 `config.base_url`（由 Factory 从 ProviderDefinition 注入）。

用 `openai.AsyncOpenAI` SDK（M1 新增依赖；非 web optional）。

### 5.4 LLMMessage → OpenAI message 转换表（修订 H）

| LLMMessage 类型 | OpenAI message |
|---|---|
| `LLMUserMessage`（text content） | `{"role": "user", "content": <text>}` |
| `LLMAssistantMessage`（仅 text） | `{"role": "assistant", "content": <text>}` |
| `LLMAssistantMessage`（含 tool_calls） | `{"role": "assistant", "content": <text or None>, "tool_calls": [{"id", "type": "function", "function": {"name", "arguments": <json str>}}]}` |
| `LLMToolResultMessage` | `{"role": "tool", "tool_call_id": <id>, "content": <text>}` |
| `system_prompt` | `{"role": "system", "content": <system_prompt>}`（**作为 messages[0]**，不用 `system` 参数） |

转换函数 `to_openai_messages(messages, system_prompt) -> list[dict]`：

- 先放 system message（即使 `system_prompt` 为空字符串也跳过——空 system 不发）
- 遍历 `LLMMessage` 列表，按上表转换
- `LLMAssistantMessage` 的 `tool_calls` 必须序列化为 JSON string（OpenAI 协议要求）
- `LLMToolResultMessage` 的 `tool_call_id` 必须与对应 assistant `tool_calls[i].id` 配对

转换函数 `to_openai_tools(tools) -> list[dict]`：

- `ToolDef` → `{"type": "function", "function": {"name", "description", "parameters": <json schema or {"type": "object", "properties": {}}>}}`
- 空 tools 返回 `None`（不传 `tools` 参数）

### 5.5 stream() 实现要点（修订 E + F + I）

```python
async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
    kwargs = self._build_kwargs(request)
    final_stop: Literal["stop", "length", "tool_use"] = "stop"
    final_usage = Usage()
    tool_buffers: dict[int, dict] = {}

    signal = request.signal
    raw_stream = None
    try:
        raw_stream = await self._client.chat.completions.create(
            **kwargs, stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in raw_stream:
            # 1. 先读 usage（OpenAI 末态 chunk 可能只有 usage）
            if chunk.usage is not None:
                final_usage = Usage(
                    input=getattr(chunk.usage, "prompt_tokens", 0) or 0,
                    output=getattr(chunk.usage, "completion_tokens", 0) or 0,
                    total_tokens=(
                        (getattr(chunk.usage, "prompt_tokens", 0) or 0)
                        + (getattr(chunk.usage, "completion_tokens", 0) or 0)
                    ),
                )

            # 2. choices 为空时 continue（usage-only chunk）
            if not chunk.choices:
                continue

            # 3. signal 检查（协作式中止）
            if signal is not None and _is_set(signal):
                yield DoneEvent(stop_reason="aborted", usage=final_usage)
                return

            choice = chunk.choices[0]
            delta = choice.delta

            # 4. text delta
            text = getattr(delta, "content", None)
            if text:
                yield TextDeltaEvent(delta=text)

            # 5. tool_call 增量累积（不立即 yield；stream 结束或 finish_reason 时 flush）
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    buf = tool_buffers.setdefault(tc.index, {
                        "id": "", "name": "", "args_parts": [],
                    })
                    if getattr(tc, "id", None):
                        buf["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            buf["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            buf["args_parts"].append(fn.arguments)

            # 6. finish_reason
            fr = getattr(choice, "finish_reason", None)
            if fr == "stop":
                final_stop = "stop"
            elif fr == "length":
                final_stop = "length"
            elif fr == "tool_calls":
                final_stop = "tool_use"

            # 7. reasoning_content 忽略（修订 I）——不 yield TextDeltaEvent，不写 Message

        # 8. stream 正常结束：flush tool_calls（按 index 排序）
        for idx in sorted(tool_buffers.keys()):
            buf = tool_buffers[idx]
            raw_json = "".join(buf["args_parts"])
            try:
                args = json.loads(raw_json) if raw_json else {}
            except Exception as e:
                raise ProviderProtocolError(
                    "tool_use input JSON parse failed",
                ) from None   # 修订 G：固定短文本，不带原始异常链
            if not isinstance(args, dict):
                raise ProviderProtocolError(
                    "tool_use input must be JSON object",
                ) from None
            yield ToolCallEvent(tool_call=ToolCall(
                id=buf["id"], name=buf["name"], arguments=args,
                raw={"id": buf["id"], "name": buf["name"], "input": args},
            ))

        yield DoneEvent(stop_reason=final_stop, usage=final_usage)

    except asyncio.CancelledError:
        # Cancellation 原样传播（修订 E）
        raise
    except ProviderError:
        # 已知 Provider 错误——向上抛，ModelClient 转 ErrorEvent
        raise
    except Exception as e:
        # 未知异常——映射为 ProviderError 子类（修订 G：固定文本）
        raise _map_unknown_exception(e) from None
    finally:
        # 修订 F：raw stream 必须 close
        if raw_stream is not None:
            try:
                await raw_stream.close()
            except Exception:
                pass
```

**修订 E 落地清单**：
- ✅ 先读 `chunk.usage`（OpenAI 末态 chunk 只有 usage）
- ✅ `chunk.choices` 为空时 `continue`（不假设 `choices[0]` 存在）
- ✅ usage 缺失不是错误（保持默认 `Usage()`）
- ✅ ToolCall buffers 按 `index` 排序后 flush
- ✅ stream 正常结束时也 flush 已完成的 tool_calls
- ✅ signal abort 后关闭 stream（`return` 触发 finally close）
- ✅ cancellation 原样传播（`asyncio.CancelledError` 不映射为 ProviderError）

### 5.6 错误映射（修订 G：固定短文本）

`_map_unknown_exception(e)` ——**只看异常类型**，不看 `str(e)` / `repr(e)` / `response.text` / `request body`：

| 异常类型（`openai.*`） | 抛 |
|---|---|
| `openai.AuthenticationError`（401） | `ProviderAuthenticationError("authentication failed")` |
| `openai.PermissionDeniedError`（403） | `ProviderAuthenticationError("permission denied")` |
| `openai.RateLimitError`（429） | `ProviderRateLimitError("rate limited")` |
| `openai.APIConnectionError` | `ProviderStreamError("connection error")` |
| `openai.APITimeoutError` | `ProviderStreamError("request timeout")` |
| `openai.BadRequestError` | `ProviderProtocolError("bad request")` |
| `openai.NotFoundError` | `ProviderProtocolError("resource not found")` |
| 其它 | `ProviderStreamError("stream error")` |

**严格禁止**：
- ❌ `str(exc)` / `repr(exc)`（OpenAI SDK 异常文本可能含 endpoint / request id）
- ❌ `getattr(exc, "response", None).text`（HTTP body 可能含 echo 回显的 request）
- ❌ 任何 request body / `messages` / `system_prompt`（用户内容敏感）
- ❌ `Authorization` header / `api_key` / `base_url`
- ❌ `from e` 保留 `__cause__` 链（避免 traceback 打印敏感上下文）

所有映射都用 `from None`（中断 cause 链）。

`ModelClient.stream` 已吞掉 `ProviderError` → `ErrorEvent`；revision `error_summary` 截断 500 chars（D2-3 既有约束）。

### 5.7 reasoning_content（修订 I：明确忽略）

某些 OpenAI-compatible 服务（如 DeepSeek / Qwen reasoning 模型）会在 delta 中返回 `reasoning_content` 字段。M1-1 **明确忽略**：

- ❌ 不生成 `TextDeltaEvent`
- ❌ 不写 `Message` / `Event` / `Revision`
- ❌ 不出现在 `usage.output_tokens` 之外的任何位置

实现：`getattr(delta, "reasoning_content", None)` 检测到时直接跳过——不抛错。

理由：
- M1 不实现 reasoning trace UI
- reasoning_content 与最终回答是分离的——保留它需要新事件类型（破坏 §1.3 contract）
- 用户看到的最终文本不应包含 reasoning

### 5.8 aclose() 生命周期（修订 F）

```python
async def aclose(self) -> None:
    if self._closed: return
    self._closed = True
    try:
        await self._client.close()
    except Exception:
        pass
```

幂等；与 `AnthropicCompatAdapter.aclose` 行为对齐。

**Stream 生命周期（修订 F）**：
- raw stream（`self._client.chat.completions.create(stream=True)` 返回值）**必须**在 finally 中 `close()`
- 或者使用 `async with` stream context manager（若 SDK 提供）
- Adapter `aclose()` 关闭 SDK client（连接池级别）
- 两者职责分离：`stream()` 内部 close raw stream；`aclose()` 关 SDK client

## 6. M1-3 `ProviderFactory` 设计（决策 4）

### 6.1 模块位置

```
src/pi_agent_core_py/providers/factory.py
```

### 6.2 签名（决策 4：返回 `ProviderAdapter`）

```python
def create_provider(
    *,
    provider_definition: ProviderDefinition,
    api_key: str,
    model_id: str,
) -> ProviderAdapter: ...
```

**返回 `ProviderAdapter`**（不返回 `ModelClient`）。`RequestProviderRuntime` 负责包装成 `ModelClient`——保持 factory 简单，runtime 控制生命周期。

### 6.3 路由逻辑（唯一知道 Adapter 映射的位置）

```python
def create_provider(*, provider_definition, api_key, model_id) -> ProviderAdapter:
    pid = provider_definition.id

    if pid == "glm":
        config = GLMConfig(
            api_key=api_key,
            base_url=provider_definition.default_base_url,
            model=model_id,
        )
        return GLMProviderAdapter(config)

    if pid == "anthropic":
        config = AnthropicCompatConfig(
            api_key=api_key,
            base_url=provider_definition.default_base_url,
            model=model_id,
        )
        return AnthropicCompatAdapter(config)

    if provider_definition.api_style == "openai_compatible":
        config = OpenAICompatConfig(
            api_key=api_key,                       # SecretStr 在 Pydantic 校验时转换
            base_url=provider_definition.default_base_url,
            model=model_id,
        )
        return OpenAICompatibleProvider(config, provider_id=pid)

    raise UnsupportedProviderError("unknown provider") from None   # 修订 G：固定文本
```

### 6.4 错误隔离

- `api_key` 为空 → `ProviderConfigError("missing api_key")`（构造阶段抛，不进 stream）
- `model_id` 为空 → `ProviderConfigError("missing model_id")`
- Provider 未知 → `UnsupportedProviderError("unknown provider")`

**所有异常 `from None`**（修订 G；中断 `__cause__` 链，避免 Strategy 异常文本含 secret 泄漏——见 `feedback_service_wrap_strategy_exc_from_none.md` memory）。

## 7. M1-4 `RequestProviderRuntime` 设计

### 7.1 模块位置

```
src/pi_agent_core_py/web/provider_runtime.py
```

### 7.2 RequestProviderSelection（修订 C）

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class RequestProviderSelection:
    profile_id: str
    provider_id: str
    model_id: str
    credential_id: str = field(repr=False)   # 不进 repr（敏感）
    selection_source: Literal["default", "explicit"]
```

**修订 C 落地**：
- 增加 `credential_id`（`repr=False`——不进日志 / print / traceback）
- `resolve_selection` 一次性冻结 Binding + Profile + Credential ID
- `build_adapter` **不再次读取 Profile**——只通过 `credential_id` 调 `CredentialService.resolve_secret_for_request()`

理由：减少 SQLite 读次数；保证请求生命周期内 Binding/Profile/Credential 不被并发修改污染。

### 7.3 CredentialService 窄接口（决策 5）

`RequestProviderRuntime` **不直接依赖 `SecretStoreRouter`**。给 `CredentialService`（E1 已存在）增加一个窄内部接口：

```python
class CredentialService:
    # ... E1 既有方法 ...

    async def resolve_secret_for_request(
        self,
        credential_id: str,
    ) -> str:
        """读 CredentialRecord + 通过 SecretStoreRouter 解析 Secret。

        内部职责：
        - credential 不存在 → CredentialNotFoundError（固定短文本）
        - SecretStore backend 不可用 → raise（固定短文本）
        - 解析失败 / secret 已撤销 → raise（固定短文本）
        - 返回 plaintext api_key（仅请求内存内有效）

        错误信息固定短文本——不暴露 secret_ref / endpoint / 原始异常。
        """
```

**为什么不让 RequestProviderRuntime 直接依赖 SecretStoreRouter**：
- 集中 Credential 安全错误映射（一处实现，多处复用）
- 避免 web 层直接持有 Secret 引用（缩小攻击面）
- E1 CredentialService 已有完整的 Record + Router + Store 解析逻辑——扩展窄接口比新建依赖更安全

### 7.4 RequestProviderRuntime 类

```python
class RequestProviderRuntime:
    """请求级 Provider 绑定——临时替换 harness.agent.client。"""

    def __init__(
        self,
        *,
        provider_config_service: ProviderConfigService,
        credential_service: CredentialService,        # E1 + 决策 5 窄接口
        provider_registry: ProviderRegistry,          # 内置 + M1-2 扩展
    ) -> None: ...

    async def resolve_selection(
        self, session_id: str
    ) -> RequestProviderSelection | None:
        """读 SessionModelBinding → Profile → 返回 selection。
        不读 Secret；返回 None 表示 Session 无 Binding。"""

    async def build_adapter(
        self, selection: RequestProviderSelection
    ) -> ProviderAdapter:
        """调 credential_service.resolve_secret_for_request(selection.credential_id)
        → api_key → factory.create_provider(...) → 返回 adapter。
        不再次读取 Profile。"""

    @asynccontextmanager
    async def bind_to_harness(
        self,
        harness: AgentHarness,
        selection: RequestProviderSelection,
    ): ...
```

### 7.5 resolve_selection：一次性冻结

```python
async def resolve_selection(self, session_id: str) -> RequestProviderSelection | None:
    binding = await self.provider_config_service.get_session_binding(session_id)
    if binding is None:
        return None   # 修订 B：Session 无 Binding → caller 使用 original client

    profile = await self.provider_config_service.get_profile(binding.profile_id)
    if profile is None:
        raise ProviderSelectionError("profile not found")   # 修订 B + G

    if profile.status != "ready":
        raise ProviderSelectionError(f"profile status: {profile.status}")   # 修订 B：固定错误，不 fallback

    return RequestProviderSelection(
        profile_id=profile.id,
        provider_id=profile.provider_id,
        model_id=binding.model_id,
        credential_id=profile.credential_id,   # repr=False
        selection_source=binding.source,
    )
```

### 7.6 build_adapter：不再次读取 Profile

```python
async def build_adapter(self, selection: RequestProviderSelection) -> ProviderAdapter:
    provider_def = self.provider_registry.get(selection.provider_id)
    if provider_def is None:
        raise ProviderSelectionError("unknown provider") from None   # 修订 G

    api_key = await self.credential_service.resolve_secret_for_request(
        selection.credential_id,
    )

    return create_provider(
        provider_definition=provider_def,
        api_key=api_key,
        model_id=selection.model_id,
    )
```

### 7.7 bind_to_harness async context manager

```python
@asynccontextmanager
async def bind_to_harness(self, harness, selection):
    adapter = await self.build_adapter(selection)
    new_client = ModelClient(adapter)
    original_client = harness.agent.client
    harness.agent.client = new_client
    try:
        yield selection
    finally:
        harness.agent.client = original_client
        try:
            await new_client.close()
        except Exception:
            pass   # 关闭失败不阻塞请求结束
```

**关键约束**：
- `original_client` 引用始终保留——请求结束后 harness 恢复到原 client
- request client 关闭失败不影响 request 已完成的返回值
- 与 `Harness.close()` 不冲突——`Harness.close()` 仍关 `original_client`

### 7.8 接入点：`_execute_prompt`（修订 A）

`bind_to_harness` 在 `_execute_prompt` 内激活一次，覆盖整个 LLM 执行窗口：

```python
async def _execute_prompt(validated, **kwargs):
    selection = await provider_runtime.resolve_selection(validated.session_id)
    if selection is None:
        # 修订 B：Session 无 Binding → 不 swap，用 original/legacy client
        return await _execute_prompt_inner(validated, **kwargs)

    try:
        async with provider_runtime.bind_to_harness(harness, selection):
            return await _execute_prompt_inner(validated, **kwargs)
    except ProviderSelectionError as e:
        # 修订 B：显式 Binding 但 Profile/Credential/Adapter 无效 → 固定错误，禁止 fallback
        raise PromptRuntimeError(...) from None   # 修订 G
```

### 7.9 Session 无 Binding 行为（修订 B）

**两种情况**：

| 情况 | 行为 |
|---|---|
| Session 无 Binding（`binding is None`） | **不 swap**，用现有 `original/legacy client`（通常是 default GLM client）。这是向后兼容路径——保留 E2 之前的行为 |
| 显式 Binding 存在，但 Profile/Credential/Adapter 无效（profile deleted / credential missing / provider unknown / profile.status != "ready"） | **固定错误**——抛 `ProviderSelectionError`（短文本）；**禁止自动 fallback** 到其他 Provider |

理由：
- 自动 fallback 会破坏请求级不可变约束（用户选了 Qwen，结果系统悄悄用 GLM，无法调试）
- 显式 Binding 失败应该让用户看到错误，而不是掩盖
- Session 无 Binding 是合法状态（用户从未配置）——保留 legacy client 是平滑迁移路径

### 7.10 单 active request lock 复用

`POST /api/prompt/async` 已有单 active request lock（P1-B1）。`provider_runtime` 不引入第二把锁——`bind_to_harness` 的生命周期**严格小于** active request lock。

### 7.11 修订决策 6：不写 context.metadata

**删除** `context.metadata["provider_selection"]`（原 M1-0 草稿中有此设计）。

理由：
- `RequestProviderSelection` 只存在于**请求内存**——不进 snapshot / session / SQLite / log
- 持久化 `provider` / `model` / `usage` 已有路径：`AssistantMessage.model_dump()` 写入 `web_message_revisions.content_json`（D2 冻结）
- 避免敏感字段（`credential_id` 即使 `repr=False`，进 metadata 仍有泄漏风险）
- 减少 snapshot metadata 膨胀

**唯一持久化路径**：`AssistantMessage` 的 `provider` / `model` / `usage` 字段（由 loop 在执行中自动填，不需要 provider_runtime 额外注入）。

## 8. M1-5 / M1-6 接线

### 8.1 M1-5：`_execute_prompt` wrap（唯一接入点）

按 §7.8 实现。`_run_prompt_core` 和 `_run_regeneration_core` 都通过 `_execute_prompt` 走到 wrap 路径。

### 8.2 M1-6：Regenerate 行为测试（不再 wrap）

M1-6 **不修改** `_run_regeneration_core` 源码（修订 A）。只加端到端行为测试：

- Regenerate 用当前 Session Binding 的 Provider/Model
- Regenerate 后 revision `content_json` 含正确的 `provider` / `model` / `usage`（来自 `AssistantMessage.model_dump()`）
- Regenerate 失败（如 401）→ revision.status="error"，不影响下个请求
- Regenerate 期间切换 Binding → 当前 regenerate 不变；下一次 regenerate 用新 Binding

### 8.3 不动 D2 不变量

D2 schema / revision / finalize 流程**完全不修改**。`web_message_revisions.content_json` 自带 `provider` / `model` / `usage`——这是 D2 既有的冻结决策（边界 A）。

### 8.4 不记 provider_selection metadata（决策 6）

`context.metadata["provider_selection"]` **不写**。详见 §7.11。

### 8.5 运行中切换不 abort（决策 7）

用户在请求运行中切换 Binding（`PUT /api/sessions/{sid}/model-binding`）：
- ✅ 当前请求继续用旧 Provider（`RequestProviderSelection` 已冻结）
- ❌ **不**主动 abort 当前请求
- ✅ 下一次 Prompt / Regenerate 用新 Binding

实现：`bind_to_harness` 内不监听 Binding 变更事件；`PUT /api/sessions/{sid}/model-binding` 不发送 abort 信号给 active request。

## 9. M1-7 测试策略

### 9.1 mock contract tests

每个 Provider（GLM / Qwen / Kimi / Anthropic）一个 mock contract test：
- 用 SDK 的 `MockTransport` / 自定义 httpx transport 拦截请求
- 验证 `stream()` 返回的 StreamEvent 序列符合契约
- 验证 `aclose()` 幂等
- 验证 raw stream 在所有路径（正常 / 异常 / abort）都被 close（修订 F）

### 9.2 行为契约（4 个 Provider 共测）

| 场景 | 期望 |
|---|---|
| 单轮 text 回答 | yield TextDeltaEvent[] → DoneEvent(stop_reason="stop", usage) |
| 单轮 tool_use | yield TextDeltaEvent[]? → ToolCallEvent → DoneEvent(stop_reason="tool_use") |
| abort 中途 | yield DoneEvent(stop_reason="aborted") |
| 401 | ProviderAuthenticationError → ModelClient 转 ErrorEvent |
| 429 | ProviderRateLimitError → ErrorEvent |
| 网络断开 | ProviderStreamError → ErrorEvent |
| 非法 tool input JSON | ProviderProtocolError → ErrorEvent |
| usage-only chunk（choices=[]） | continue；不抛错（修订 E） |
| chunk.usage 缺失 | 用默认 `Usage()`；不抛错（修订 E） |
| reasoning_content 出现 | 忽略；不生成 TextDeltaEvent（修订 I） |
| asyncio.CancelledError | 原样传播；不映射为 ProviderError（修订 E） |

### 9.3 错误信息固定短文本（修订 G）

测试断言：
- `ProviderAuthenticationError.message` 不含 `api_key` / `Authorization` / endpoint URL
- `ProviderError` 子类的 `__cause__` 是 None（`from None` 生效）
- ErrorEvent.message 不含原始异常的 `str()` / `repr()`
- request body / response body 不出现在任何 ErrorEvent

### 9.4 切换只影响下次请求（决策 7）

```
Session A 绑定 GLM → 启动 Prompt → 运行中
用户切换到 Qwen → PUT /api/sessions/A/model-binding → 200
当前 Prompt 仍用 GLM 完成
下一次 Prompt 用 Qwen
```

通过 `RequestProviderSelection` 不可变快照验证。

### 9.5 失败不污染下一请求（修订 B）

```
Session A 绑定 Qwen → Qwen 401（invalid key） → ErrorEvent
用户切换到 GLM → PUT /api/sessions/A/model-binding → 200
下一次 Prompt 用 GLM → 正常执行
```

`bind_to_harness` 的 finally 必须保证 `harness.agent.client` 还原到 original_client。

**显式 Binding 失败不 fallback**：若 Qwen Profile 失效（status != "ready"），直接抛 `ProviderSelectionError`——不偷偷用 GLM。

### 9.6 Core Runtime diff = 0

- `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`：**不修改**
- `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py`：**不修改**
- `providers/registry.py`：**新增 qwen / kimi presets**（M1-2 允许；不动既有 _GLM / _ANTHROPIC）
- `providers/factory.py` / `providers/openai_compat.py`：**新增**
- `model_client.py`：**不修改**（thin wrapper 已通用）
- `web/app.py`：**仅 `_execute_prompt` 加 wrap + lifespan 装配 provider_runtime**（不大规模重构）
- `web/credentials_service.py`（或 E1 既有 service 模块）：**新增 `resolve_secret_for_request` 方法**（决策 5）

## 10. 显式不包含（M1 范围之外）

- ❌ Custom Base URL / Custom Provider
- ❌ 远程模型目录（`/v1/models`）调用
- ❌ Provider health / 限流状态 / 成本统计
- ❌ 自动 provider fallback / 负载均衡
- ❌ Qwen 海外区域 / Workspace 专属 endpoint（仅华北 2 北京共享）
- ❌ 前端切换 UI（M2）
- ❌ 最终 security freeze（M3）
- ❌ 多 Key / 同 Provider 多 Profile 管理 UI（M2 之后）
- ❌ reasoning trace UI / 持久化（修订 I）
- ❌ `_run_regeneration_core` 源码修改（修订 A——M1-6 仅加测试）

## 11. 冻结决策（11 项）

| # | 决策 | 实现 |
|---|---|---|
| 1 | Qwen `default_base_url = https://dashscope.aliyuncs.com/compatible-mode/v1`；仅华北 2 北京共享 endpoint | `registry.py` M1-2 |
| 2 | Kimi `default_base_url = https://api.moonshot.cn/v1` | `registry.py` M1-2 |
| 3 | `OpenAICompatConfig` 不保存 `provider_id`；`OpenAICompatibleProvider` 构造参数传入，设实例属性 | `openai_compat.py` M1-1 |
| 4 | `ProviderFactory.create_provider` 返回 `ProviderAdapter`；`RequestProviderRuntime` 包装 `ModelClient` | `factory.py` M1-3 + `provider_runtime.py` M1-4 |
| 5 | `RequestProviderRuntime` 不直接依赖 `SecretStoreRouter`；`CredentialService.resolve_secret_for_request(credential_id) -> str` 窄接口 | `credentials_service.py` M1-4 扩展 + `provider_runtime.py` |
| 6 | **删除** `context.metadata["provider_selection"]`；`RequestProviderSelection` 只存在于请求内存；持久化走 `AssistantMessage` | `provider_runtime.py` M1-4 |
| 7 | 运行中切换 Binding 不 abort 当前请求，只影响下次 Prompt / Regenerate | `provider_runtime.py` M1-4 + `provider_profiles_api.py` 不改 |

## 12. 修订缺口（A-I）

### 12.1 修订 A：`_execute_prompt` 是唯一接入点

`bind_to_harness` 只在 `_execute_prompt` 内激活一次。`_run_regeneration_core` **不**再次 wrap。M1-6 只增加 Regenerate 行为测试，不改源码。

详见 §3 + §8.2。

### 12.2 修订 B：Session 无 Binding 用 legacy client；显式 Binding 失败固定错误

| 情况 | 行为 |
|---|---|
| Session 无 Binding | 不 swap，用 `original/legacy client` |
| 显式 Binding 但 Profile/Credential/Adapter 无效 | 抛 `ProviderSelectionError`（短文本）；**禁止自动 fallback** |

详见 §7.8 + §7.9 + §9.5。

### 12.3 修订 C：`RequestProviderSelection` 含 `credential_id`

```python
@dataclass(frozen=True)
class RequestProviderSelection:
    profile_id: str
    provider_id: str
    model_id: str
    credential_id: str = field(repr=False)   # 不进 repr
    selection_source: Literal["default", "explicit"]
```

`resolve_selection` 一次性冻结 Binding + Profile + Credential ID；`build_adapter` 不再读 Profile。

详见 §7.2 + §7.5 + §7.6。

### 12.4 修订 D：`OpenAICompatConfig` 精简

```python
class OpenAICompatConfig(BaseModel):
    api_key: SecretStr = Field(repr=False)
    base_url: str
    model: str
    timeout_s: float = 60.0
    max_tokens: int | None = 4096
    temperature: float | None = None
```

删除 `extra_headers`；`api_key` 用 `SecretStr`；`max_tokens` / `temperature` 允许 `None`。

详见 §5.2。

### 12.5 修订 E：Stream parsing 7 项

- 先读 `chunk.usage`
- `choices` 为空时 `continue`
- 不假设 `choices[0]` 永远存在
- usage 缺失不是错误
- ToolCall buffers 按 `index` 排序
- stream 正常结束时也 flush 已完成 tool calls
- signal abort 后关闭 stream
- `asyncio.CancelledError` 原样传播

详见 §5.5。

### 12.6 修订 F：Stream 生命周期

- raw stream 必须 `try/finally close` 或用 `async with`
- Adapter `aclose()` 继续幂等关闭 SDK client

详见 §5.5（finally 段）+ §5.8。

### 12.7 修订 G：固定安全错误

- 不使用 `str(exc)` / `repr(exc)` / `response.text` / request body
- `ProviderError` message 使用固定短文本
- 所有映射用 `from None`

详见 §5.5（异常映射段）+ §5.6 + §6.4。

### 12.8 修订 H：LLMMessage → OpenAI 转换表

5 种转换：user text / assistant text / assistant tool_calls / tool result + tool_call_id / system prompt。

详见 §5.4。

### 12.9 修订 I：`reasoning_content` 明确忽略

不生成 `TextDeltaEvent`，不写 `Message` / `Event` / `Revision`。

详见 §5.7。

## 13. M1 实施顺序（user 审核已通过）

```
M1-0 Provider Contract Audit（本文档） ✅ DESIGN FROZEN
        ↓
M1-1 providers/openai_compat.py + 测试（含 §5 全部约束）
        ↓
M1-2 registry.py 加 qwen / kimi presets + 测试
        ↓
M1-3 providers/factory.py + 测试
        ↓
M1-4 web/provider_runtime.py + credentials_service.resolve_secret_for_request + 测试
        ↓
M1-5 _execute_prompt wrap（唯一接入点）+ 测试
        ↓
M1-6 Regenerate 行为测试（不改 _run_regeneration_core 源码）
        ↓
M1-7 端到端 Runtime tests + Core Runtime diff 校验
        ↓
停止并审核，进入 M2 Frontend Switching
```

每个子阶段一个原子 commit，commit message 格式：

```
feat(providers): add openai-compatible provider adapter
feat(providers): add qwen and kimi provider presets
feat(providers): add provider factory
feat(web): add request-scoped provider runtime
feat(web): bind prompt execution to session provider
test(web): cover regenerate provider switching behavior
test(providers): validate multi-provider runtime
```

---

**DESIGN FROZEN @ 2026-07-19** — 进入 M1-1 `providers/openai_compat.py` 编码。
