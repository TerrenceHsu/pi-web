# P1-E M1 — Multi-Provider Runtime Design

> **状态**：M1-0 Provider Contract Audit（DESIGN DRAFT，待 user 审核）
> **基线**：master `3a1e011`（P1-E2 Backend Foundation ✅ FROZEN @ `cad7ca7` + Pivot docs committed）
> **日期**：2026-07-19
> **前置**：[p1-e2-provider-profiles.md §19 Pivot 附录](p1-e2-provider-profiles.md)
> **范围**：只读审计现有 Provider contract + 冻结 M1 实施接口；不写生产代码

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
    provider_id: str = ""   # 类属性
    model: str = ""         # 类属性

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

**M1 决策**：`ProviderFactory` 返回 `ProviderAdapter`，`provider_runtime` 包装成 `ModelClient`。理由：保持 factory 简单，runtime 负责 ModelClient 生命周期。

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

**M1-4 `bind_to_harness`** 沿用此模式：

```python
@asynccontextmanager
async def bind_to_harness(harness, selection: RequestProviderSelection):
    new_adapter = factory.create_provider(...)
    new_client = ModelClient(new_adapter)
    original_client = harness.agent.client
    harness.agent.client = new_client
    try:
        yield selection
    finally:
        harness.agent.client = original_client
        await new_client.close()   # 关闭 request client
```

## 3. _run_prompt_core / _run_regeneration_core 审计

### 3.1 真正的执行入口：`_execute_prompt`

`_run_prompt_core`（`web/app.py:1280-1292`）是 D2-4 thin wrapper：

```python
async def _run_prompt_core(validated: _PromptValidated) -> PromptRunOutcome:
    execution = await _execute_prompt(validated)
    return await _persist_normal_prompt_result(validated, execution)
```

`_execute_prompt` 是真正调用 harness 的位置——**M1-5 接入点在这里**（在调用 harness 之前 wrap `bind_to_harness`）。

### 3.2 _run_regeneration_core 的临时替换范例（`web/app.py:1334-1434+`）

```python
async def _run_regeneration_core(validated, *, assistant_message_id, request_id, revision_id=None):
    # 1. create_running_revision（短事务）
    # 2. 读 canonical → 截断到 preceding user
    original_harness_messages = list(harness.agent.state.messages)
    # 3. 临时替换 harness state
    harness.agent.state.messages = list(regeneration_history)
    try:
        execution = await _execute_prompt(
            validated,
            override_initial_messages=regeneration_history,
            suppress_user_append=True,
        )
    except PromptRuntimeError as e:
        await ext_store.mark_revision_error(...)
        await _reset_harness_to_session(store, session_id, original_harness_messages)
        raise
    # 4. _persist_regeneration_result（finalize_revision + snapshot）
    # 5. _reset_harness_to_session（无论成败都要 reset）
```

**M1-6 接入点**：`_execute_prompt` 之前同样 wrap `bind_to_harness`；regenerate 用当前 Session Binding（不动 D2 不变量；revision `content_json` 自带 model 信息）。

### 3.3 事务边界（D2-4 不变量）

D2-4 保证：**短事务 create_running_revision → 释放 → 长 LLM 执行 → finalize 短事务**——没有任何 SQLite `BEGIN IMMEDIATE` 跨越 LLM 调用。

M1-4 `bind_to_harness` 在 LLM 执行窗口内激活——**不跨 SQLite 事务**，与 D2-4 不变量兼容。

## 4. ProviderDefinition 现状 + M1-2 扩展

### 4.1 当前 `_DEFAULT_REGISTRY`（`registry.py:239-241`）

```python
_DEFAULT_REGISTRY = ProviderRegistry((_GLM_DEFINITION, _ANTHROPIC_DEFINITION))
```

两者都是 `api_style="anthropic_compatible"`。

### 4.2 `api_style` 已预留 `openai_compatible`

```python
ProviderAPIStyle = Literal["anthropic_compatible", "openai_compatible"]
```

`registry.py:24` 文档注释：

> `openai_compatible` 和 `custom` 留待 P1-E2 ProviderProfile 引入.

E2 没引入（按 Pivot 决策）—— **M1-2 顺势引入 `qwen` / `kimi`**，符合 E2 原设计意图。

### 4.3 M1-2: Qwen / Kimi presets（建议）

```python
_QWEN_DEFINITION = ProviderDefinition(
    id="qwen",
    display_name="Alibaba Qwen (OpenAI-compatible)",
    api_style="openai_compatible",
    default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    credential_validation_strategy="unsupported",  # M1 不做远端 Key validation
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

### 5.2 OpenAICompatConfig（Pydantic BaseModel，对齐 `AnthropicCompatConfig`）

```python
class OpenAICompatConfig(BaseModel):
    api_key: str
    base_url: str           # 必填——由 factory 从 ProviderDefinition.default_base_url 注入
    model: str              # 必填——用户输入或 Profile.default_model
    timeout_s: float = 60.0
    max_tokens: int = 4096
    temperature: float | None = 0.0
    extra_headers: dict[str, str] = Field(default_factory=dict)
```

`provider_id` 不在 Config 内——由 `OpenAICompatibleProvider` 子类化或构造参数覆盖（Qwen / Kimi 共用同一 Adapter，但 `provider_id` 不同）。

### 5.3 OpenAICompatibleProvider 类

```python
class OpenAICompatibleProvider(ProviderAdapter):
    def __init__(
        self,
        config: OpenAICompatConfig,
        *,
        provider_id: str,                # "qwen" / "kimi"
        client: AsyncOpenAI | None = None,   # 测试注入
    ) -> None: ...

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]: ...
    async def aclose(self) -> None: ...
```

用 `openai.AsyncOpenAI` SDK（M1 新增依赖；非 web optional）。

### 5.4 stream() 实现要点

1. 构造 OpenAI Chat Completions 请求（`messages` / `tools` / `model` / `temperature` / `max_tokens` / `stream=True`）
2. `LLMMessage → OpenAI messages` 转换（参考 `to_anthropic_messages`）
3. `ToolDef → OpenAI tools` 转换（参考 `to_anthropic_tools`，schema 用 JSON Schema）
4. 调 `await self._client.chat.completions.create(..., stream=True)`
5. 遍历 SSE chunks：
   - `choices[0].delta.content` → `TextDeltaEvent`
   - `choices[0].delta.tool_calls[].function.arguments` 增量累积 → `ToolCallEvent`（chunk 完成时一次 yield）
   - `choices[0].finish_reason` → 决定 `DoneEvent.stop_reason`（`stop` / `length` / `tool_calls` → `tool_use`）
6. 末态 `usage`（OpenAI `stream_options={"include_usage": True}`）→ `DoneEvent.usage`
7. `signal.is_set()` 时 yield `DoneEvent(stop_reason="aborted")`

### 5.5 tool_call 增量解析

OpenAI tool_calls 是分片返回的（`function.arguments` 是 partial JSON string），需要：

```python
tool_call_buffers: dict[int, {id, name, args_parts: list[str]}] = {}

for chunk in stream:
    for tc in chunk.choices[0].delta.tool_calls or []:
        buf = tool_call_buffers.setdefault(tc.index, {...})
        if tc.id: buf["id"] = tc.id
        if tc.function.name: buf["name"] = tc.function.name
        if tc.function.arguments: buf["args_parts"].append(tc.function.arguments)

# finish_reason == "tool_calls" 时：
for buf in tool_call_buffers.values():
    args = json.loads("".join(buf["args_parts"])) if buf["args_parts"] else {}
    yield ToolCallEvent(tool_call=ToolCall(id=buf["id"], name=buf["name"], arguments=args, raw=...))
```

### 5.6 错误映射（关键安全约束）

| HTTP / SDK 异常 | 抛 |
|---|---|
| 401 / 403（`AuthenticationError`） | `ProviderAuthenticationError` |
| 429（`RateLimitError`） | `ProviderRateLimitError` |
| 网络异常 / 连接断（`APIConnectionError` / `APITimeoutError`） | `ProviderStreamError` |
| 解析失败 / 非法 tool input JSON | `ProviderProtocolError` |
| Config 缺 api_key / model 空 | `ProviderConfigError`（构造阶段抛） |

**错误信息必须**：
- ✅ 只含 `type(e).__name__: 简短描述`（如 `ProviderAuthenticationError: 401 Unauthorized`）
- ❌ 不含 `Authorization` header / 完整 `api_key`
- ❌ 不含 request body 原文（可能含 system_prompt / 用户消息——按现有 GLM Adapter 一致，不进 ErrorEvent.message）
- ❌ 不含完整 endpoint URL（最多 scheme+host）

`ModelClient.stream` 已吞掉异常文本 → `ErrorEvent`；revision `error_summary` 截断 500 chars（D2-3 既有约束）。

### 5.7 aclose() 生命周期

```python
async def aclose(self) -> None:
    if self._closed: return
    self._closed = True
    try:
        await self._client.close()
    except Exception: pass
```

幂等；与 `AnthropicCompatAdapter.aclose` 行为对齐。

## 6. M1-3 `ProviderFactory` 设计

### 6.1 模块位置

```
src/pi_agent_core_py/providers/factory.py
```

### 6.2 签名

```python
def create_provider(
    *,
    provider_definition: ProviderDefinition,
    api_key: str,
    model_id: str,
) -> ProviderAdapter: ...
```

### 6.3 路由逻辑（唯一知道 Adapter 映射的位置）

```python
def create_provider(*, provider_definition, api_key, model_id) -> ProviderAdapter:
    pid = provider_definition.id

    if pid == "glm":
        config = GLMConfig(api_key=api_key, base_url=provider_definition.default_base_url, model=model_id)
        return GLMProviderAdapter(config)

    if pid == "anthropic":
        config = AnthropicCompatConfig(api_key=api_key, base_url=provider_definition.default_base_url, model=model_id)
        return AnthropicCompatAdapter(config)

    if provider_definition.api_style == "openai_compatible":
        config = OpenAICompatConfig(api_key=api_key, base_url=provider_definition.default_base_url, model=model_id)
        return OpenAICompatibleProvider(config, provider_id=pid)

    raise UnsupportedProviderError(f"unknown provider: {pid}") from None
```

### 6.4 错误隔离

- `api_key` 为空 → `ProviderConfigError`（构造阶段抛，不进 stream）
- `model_id` 为空 → `ProviderConfigError`
- Provider 未知 → `UnsupportedProviderError`

**所有异常 `from None`**（中断 `__cause__` 链，避免 Strategy 异常文本含 secret 泄漏——见 `feedback_service_wrap_strategy_exc_from_none.md` memory）。

## 7. M1-4 `web/provider_runtime.py` 设计

### 7.1 模块位置

```
src/pi_agent_core_py/web/provider_runtime.py
```

### 7.2 RequestProviderSelection（不可变快照）

```python
@dataclass(frozen=True)
class RequestProviderSelection:
    profile_id: str
    provider_id: str
    model_id: str
    selection_source: Literal["default", "explicit"]   # Session Binding source
```

请求启动时创建，**整个请求生命周期不变**。

### 7.3 RequestProviderRuntime 类

```python
class RequestProviderRuntime:
    """请求级 Provider 绑定——临时替换 harness.agent.client。"""

    def __init__(
        self,
        *,
        provider_config_service: ProviderConfigService,
        credential_service: CredentialService,   # E1 安全接口
        secret_router: SecretStoreRouter,        # E1
        provider_registry: ProviderRegistry,     # 内置 + M1-2 扩展
    ) -> None: ...

    async def resolve_selection(
        self, session_id: str
    ) -> RequestProviderSelection:
        """读 SessionModelBinding → Profile → Credential → ProviderDefinition。
        不读 Secret；返回 selection（含 profile_id / provider_id / model_id / source）。
        """

    async def build_adapter(
        self, selection: RequestProviderSelection
    ) -> ProviderAdapter:
        """读 Secret（仅此时）→ Factory.create_provider → 返回 adapter。"""

    @asynccontextmanager
    async def bind_to_harness(
        self,
        harness: AgentHarness,
        selection: RequestProviderSelection,
    ):
        """临时替换 harness.agent.client；finally 还原 + close request client。"""
```

### 7.4 接入点

`provider_runtime.bind_to_harness` 在 `_execute_prompt` 调用**之前**激活，覆盖整个 LLM 执行窗口：

```python
# _execute_prompt 改造（伪代码）
async def _execute_prompt(validated, **kwargs):
    selection = await provider_runtime.resolve_selection(validated.session_id)
    async with provider_runtime.bind_to_harness(harness, selection):
        # 原有 _execute_prompt 主体（调 harness.run_prompt / run_continue）
        ...
```

### 7.5 finally 恢复 + close 语义

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
- `original_client` 引用始终保留——请求结束后 harness 恢复到原 client（通常是 default GLM client）
- request client 关闭失败不影响 request 已完成的返回值
- 与 `Harness.close()` 不冲突——`Harness.close()` 仍关 `original_client`

### 7.6 单 active request lock 复用

`POST /api/prompt/async` 已有单 active request lock（P1-B1）。`provider_runtime` 不引入第二把锁——`bind_to_harness` 的生命周期**严格小于** active request lock。

## 8. M1-5 / M1-6 Prompt + Regenerate 接线

### 8.1 `_run_prompt_core` 接入

在 `_execute_prompt` 内 wrap：

```python
async def _execute_prompt(validated, **kwargs):
    selection = await provider_runtime.resolve_selection(validated.session_id)
    async with provider_runtime.bind_to_harness(harness, selection):
        # 原有 _execute_prompt 主体
        return await _execute_prompt_inner(validated, **kwargs)
```

### 8.2 `_run_regeneration_core` 接入

同样在 `_execute_prompt` 之前 wrap——regenerate 用当前 Session Binding（不动 D2 不变量）：

```python
# _run_regeneration_core 改造（伪代码）
selection = await provider_runtime.resolve_selection(validated.session_id)
async with provider_runtime.bind_to_harness(harness, selection):
    execution = await _execute_prompt(
        validated,
        override_initial_messages=regeneration_history,
        suppress_user_append=True,
    )
```

revision `content_json` 自带 `provider` / `model` 字段——D2 已冻结，无需改 schema。

### 8.3 request metadata 记录（不含 secret）

写入 `context.metadata["provider_selection"]`：

```python
{
    "profile_id": "...",
    "provider_id": "qwen",
    "model_id": "qwen-plus",
    "selection_source": "explicit",
}
```

**不记**：
- `credential_id`（敏感）
- `api_key` / `secret_ref` / Authorization
- 内部 endpoint URL

## 9. M1-7 测试策略

### 9.1 mock contract tests

每个 Provider（GLM / Qwen / Kimi / Anthropic）一个 mock contract test：
- 用 SDK 的 `MockTransport` / 自定义 httpx transport 拦截请求
- 验证 `stream()` 返回的 StreamEvent 序列符合契约
- 验证 `aclose()` 幂等

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

### 9.3 切换只影响下次请求

```
Session A 绑定 GLM → 启动 Prompt → 运行中
用户切换到 Qwen → PUT /api/sessions/A/model-binding → 200
当前 Prompt 仍用 GLM 完成
下一次 Prompt 用 Qwen
```

通过 `RequestProviderSelection` 不可变快照验证。

### 9.4 失败不污染下一请求

```
Session A 绑定 Qwen → Qwen 401（invalid key） → ErrorEvent
下一次 Prompt 用 GLM（用户切换后）→ 正常执行
```

`bind_to_harness` 的 finally 必须保证 `harness.agent.client` 还原到 original_client。

### 9.5 Core Runtime diff = 0

- `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`：**不修改**
- `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py`：**不修改**
- `providers/registry.py`：**新增 qwen / kimi presets**（M1-2 允许）
- `providers/factory.py` / `providers/openai_compat.py`：**新增**
- `model_client.py`：**不修改**（thin wrapper 已通用）
- `web/app.py`：**仅 _execute_prompt 加 wrap**（不大规模重构）

## 10. 显式不包含（M1 范围之外）

- ❌ Custom Base URL / Custom Provider
- ❌ 远程模型目录（`/v1/models`）调用
- ❌ Provider health / 限流状态 / 成本统计
- ❌ 自动 provider fallback / 负载均衡
- ❌ 前端切换 UI（M2）
- ❌ 最终 security freeze（M3）
- ❌ 多 Key / 同 Provider 多 Profile 管理 UI（M2 之后）

## 11. 待 user 审核的关键决策点

1. **Qwen / Kimi 的 `default_base_url`** —— 用阿里 DashScope 和 Moonshot 官方 endpoint（§4.3）；若 user 有其他偏好请指明。
2. **`OpenAICompatConfig` 是否需要 `provider_id` 字段** —— 当前建议由构造参数传入（Qwen / Kimi 共用 Config）；若 user 希望放 Config 内请说明。
3. **`ProviderFactory.create_provider` 返回类型** —— 当前建议返回 `ProviderAdapter`，由 `provider_runtime` 包装成 `ModelClient`；若 user 希望直接返回 `ModelClient` 请说明。
4. **`provider_runtime` 是否复用 `ProviderConfigService` 的 Credential 安全接口** —— 当前建议复用 E1 接口（不绕过 SecretStoreRouter）；若 user 希望直接读 Credential row 请说明（不推荐，破坏 E1 安全边界）。
5. **request metadata 字段名** —— 当前建议 `context.metadata["provider_selection"]`；可调整为 `context.metadata["provider"]` 或其他。
6. **abort 期间的 provider 切换** —— 当前 `bind_to_harness` 期间切换只更新 Binding（不立即生效）；若 user 希望切换时主动 abort 当前请求请说明（不推荐，破坏请求级不可变约束）。

## 12. M1 实施顺序（user 审核通过后）

```
M1-0 Provider Contract Audit（本文档） ✅
        ↓ user 审核
M1-1 providers/openai_compat.py + 测试
        ↓
M1-2 registry.py 加 qwen / kimi presets + 测试
        ↓
M1-3 providers/factory.py + 测试
        ↓
M1-4 web/provider_runtime.py + 测试
        ↓
M1-5 _execute_prompt wrap（Prompt integration）+ 测试
        ↓
M1-6 _run_regeneration_core wrap（Regenerate integration）+ 测试
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
feat(web): bind regenerate execution to session provider
test(providers): validate multi-provider runtime
```

---

**审核完成后**：标注本文档 DESIGN FROZEN @ `<date>`，进入 M1-1 编码。
