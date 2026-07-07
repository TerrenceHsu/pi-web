"""MCP Prompts——Step 19 新增。

MCP server 可以暴露「可复用 prompt 模板」（spec 中的 Prompts capability）。
本模块负责：

1. **类型层**：`MCPPromptArgument` / `MCPPromptInfo` / `MCPPromptMessage` /
   `MCPPromptResult`——对应 `prompts/list` 和 `prompts/get` 的协议数据。
2. **Skill 适配器**：`MCPPromptSkillAdapter`——把 MCP prompt 包装成项目
   `Skill` 注册到 `SkillRegistry`，与本地 SKILL.md 加载的 Skill 同构。

```text
MCP Server
↓
prompts/list   →  list[MCPPromptInfo]
prompts/get    →  MCPPromptResult (messages: list[MCPPromptMessage])
↓
MCPPromptSkillAdapter.load_prompt_as_skill
↓
Skill(name="mcp_prompt__{server}__{prompt}", prompt=..., metadata={source:mcp_prompt,...})
↓
SkillRegistry
↓
AgentHarness.render_system_prompt
```

Step 19 简化：

- 只解析 text content。MCP prompt message 的 content 可能是字符串或
  `{"type": "text", "text": "..."}` / `{"type": "image", ...}`；非 text
  内容转占位说明，不抛错。
- 不实现 notifications/progress/logging、不实现 resources、不实现 subscribe。
- 错误处理沿用 `MCPClient` / `MCPRegistry` 当前 MCP 异常语义
  （`MCPProtocolError` / `MCPConnectionError` 等）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..skills import PromptTemplate, Skill

if TYPE_CHECKING:
    # 仅用于类型注解——避免 client.py ↔ prompts.py 之间的循环 import
    from .client import MCPClient
    from .registry import MCPRegistry


# ============================================================================
# prompts/list / prompts/get 数据类型
# ============================================================================


class MCPPromptArgument(BaseModel):
    """MCP prompt 的一个参数声明（来自 prompts/list）。

    MCP 规范允许 prompt 暴露参数；用户在 prompts/get 时传实参。
    Step 19 只把 arguments 当作声明性 metadata——不做参数 schema 校验，
    不渲染模板（很多 server 已经在 server 端渲染好返回）。
    """
    name: str
    description: str = ""
    required: bool = False


class MCPPromptInfo(BaseModel):
    """`prompts/list` 返回的单个 prompt 元信息。"""
    name: str
    description: str = ""
    arguments: list[MCPPromptArgument] = Field(default_factory=list)


class MCPPromptMessage(BaseModel):
    """`prompts/get` 返回的单条消息。

    role: MCP 规范允许 "user" / "assistant" / "system"；本字段不做枚举限制，
    保留为 str，转换 Skill 时按 role 拼装。
    content: 已经被客户端解析过的纯文本字符串（image / resource 类被转占位）。
    """
    role: str
    content: str


class MCPPromptResult(BaseModel):
    """`prompts/get` 的解析结果。

    - description：来自 server 的 prompt 描述；空时调用方可以回退到
      MCPPromptInfo.description
    - messages：已解析的 MCPPromptMessage 列表
    - raw：server 原始返回（dict | None）——给 UI / observability 用，
      不进入 Skill.prompt（避免把巨型 raw 塞进 system_prompt）
    """
    description: str = ""
    messages: list[MCPPromptMessage] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


# ============================================================================
# Content 解析（只支持 text；其它类型转占位）
# ============================================================================


def _parse_message_content(content: Any) -> str:
    """把 MCP prompt message 的 content 字段统一转成 str。

    MCP 规范允许 content 是：
      - str                        —— 直接用
      - {"type": "text", "text": "..."}  —— 取 text 字段
      - {"type": "image", ...}     —— 转 "(unsupported content type: 'image')"
      - list[...]                  —— 逐项解析后拼回

    Step 19 简化：非 text 都转占位说明，不抛错。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get("type")
        if t == "text":
            return str(content.get("text", ""))
        return f"(unsupported content type: {t!r})"
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = _parse_message_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return f"(unsupported content shape: {type(content).__name__})"


# ============================================================================
# Skill 名 namespace（独立于 MCP tool 的 64 字符限制）
# ============================================================================


#: MCP prompt skill name 字符上限。Skill name 不进入 provider tool schema，
#: 因此可以略长（不会撞到 Anthropic / OpenAI 的 64 限制）。但仍需有上限，
#: 避免极端情况下 session JSON 失控。
MAX_MCP_PROMPT_SKILL_NAME_LEN = 128


def make_mcp_prompt_skill_name(server_name: str, prompt_name: str) -> str:
    """拼 `mcp_prompt__{server}__{prompt}` 并校验长度。

    与 make_namespaced_tool_name 不同——Skill 不进 provider tool schema，
    限制更宽松（128 而非 64）。server / prompt name 字符集仍要求
    `[A-Za-z0-9_-]+`，与 MCP server / tool name 一致，避免在 UI /
    session JSON 里出现奇怪字符。
    """
    from .naming import MCP_NAME_RE, validate_namespace_part

    validate_namespace_part(kind="server_name", value=server_name)
    validate_namespace_part(kind="prompt name", value=prompt_name)
    # 双重校验：validate_namespace_part 已经查空串；这里再查一次匹配，
    # 防御外部传入 Unicode 等 validate_namespace_part 没覆盖的场景
    if not MCP_NAME_RE.match(server_name) or not MCP_NAME_RE.match(prompt_name):
        raise ValueError(
            f"invalid server/prompt name pair: {server_name!r}, {prompt_name!r}; "
            "only [A-Za-z0-9_-] allowed"
        )
    name = f"mcp_prompt__{server_name}__{prompt_name}"
    if len(name) > MAX_MCP_PROMPT_SKILL_NAME_LEN:
        raise ValueError(
            f"MCP prompt skill name too long ({len(name)} > "
            f"{MAX_MCP_PROMPT_SKILL_NAME_LEN}): {name!r}"
        )
    return name


# ============================================================================
# MCPPromptSkillAdapter
# ============================================================================


class MCPPromptSkillAdapter:
    """把 MCP prompt 转成 `Skill`，注册进 `SkillRegistry`。

    使用方式：

        adapter = MCPPromptSkillAdapter(registry)
        skills = await adapter.load_all_prompts_as_skills()
        harness.attach_skills(skills)

    生成 Skill 的字段约定：

        name         = "mcp_prompt__{server}__{prompt}"
        description  = prompt_result.description or prompt_info.description
        prompt       = PromptTemplate（body 为拼装后的多 role 消息文本）
        tags         = ["mcp", "prompt", server_name]
        status       = "enabled"
        metadata     = {
            "source": "mcp_prompt",
            "server": server_name,
            "prompt": prompt_name,
            "loader": "MCPPromptSkillAdapter",
            "arguments": [arg.model_dump() for arg in prompt_info.arguments],
        }

    注意：

    - 不会注册进任何 SkillRegistry；只返回 Skill 列表，调用方决定怎么用。
    - 同一 prompt 多次加载返回的 Skill 是新实例（不会复用缓存）。
    - server 没暴露该 prompt / prompts/get 失败 → MCPError 子类穿透抛出。
    - `load_all_prompts_as_skills` 单失败被记录到 `self.last_errors`（P1 修订）；
      调用方可读 `adapter.last_errors` 拿到 {server, prompt, error} 列表。
    """

    def __init__(self, registry: MCPRegistry) -> None:
        self.registry = registry
        # load_all_prompts_as_skills 中被吞掉的错误，按发生顺序记录。
        # 每次 load_all_prompts_as_skills 调用开头会清空——保留最近的快照。
        self.last_errors: list[dict[str, str]] = []

    async def load_prompt_as_skill(
        self,
        *,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        prompt_info: MCPPromptInfo | None = None,
    ) -> Skill:
        """拉取单个 MCP prompt 并转成 Skill。

        - prompt_info 由调用方提供时直接用（避免再发一次 prompts/list）；
          否则用 client.list_prompts() 自查（额外一次 round-trip）。
        - 用 get_prompt() 拿渲染后的 messages
        - description 兜底：prompt_result → prompt_info → 默认文案

        server_name 不在当前 registry 配置中 → ValueError
        server_name 配置但未 connected / prompts/list 失败 → MCPError 子类
        """
        client = self.registry.get_client(server_name)
        if client is None:
            raise ValueError(
                f"MCPPromptSkillAdapter: server {server_name!r} not connected "
                f"or not in registry"
            )

        # P2 修订：调用方已知 prompt_info 时直接用，避免每次再 list_prompts
        if prompt_info is None:
            prompt_info = await self._lookup_prompt_info(
                client, server_name, prompt_name,
            )
        prompt_result = await client.get_prompt(
            prompt_name, arguments=arguments, include_raw=True,
        )

        description = (
            prompt_result.description
            or (prompt_info.description if prompt_info else "")
            or f"MCP prompt {prompt_name} from {server_name}"
        )
        body = _render_prompt_messages_to_body(
            server_name=server_name,
            prompt_name=prompt_name,
            messages=prompt_result.messages,
        )

        skill_name = make_mcp_prompt_skill_name(server_name, prompt_name)
        return Skill(
            name=skill_name,
            description=description,
            prompt=PromptTemplate(
                name=skill_name,
                template=body,
                variables=[],
            ),
            tags=["mcp", "prompt", server_name],
            tool_names=[],
            metadata={
                "source": "mcp_prompt",
                "server": server_name,
                "prompt": prompt_name,
                "loader": "MCPPromptSkillAdapter",
                "arguments": (
                    [a.model_dump() for a in prompt_info.arguments]
                    if prompt_info else []
                ),
            },
        )

    async def load_all_prompts_as_skills(
        self,
        *,
        arguments_by_prompt: dict[str, dict[str, Any]] | None = None,
    ) -> list[Skill]:
        """遍历当前 registry 中所有已 refresh 的 prompts，逐个转成 Skill。

        arguments_by_prompt：可选——key 是 prompt name（**不含 server 前缀**），
        value 是传给 prompts/get 的 arguments。同名 prompt 在多个 server 上
        都会拿到同一份 arguments。

        单个 prompt 加载失败（如 server 报错）不影响其它——失败被吞掉，
        错误记录到 self.last_errors。返回的列表只含成功加载的 Skill。
        如需严格模式，调用方应自己逐个调 load_prompt_as_skill。
        """
        # P1 修订：每次 load_all 开头清空 last_errors，保留最近一次调用的快照
        self.last_errors = []
        out: list[Skill] = []
        arguments_by_prompt = arguments_by_prompt or {}
        # P2 修订：直接把 registry.list_prompts() 返回的 info 传下去，
        # 避免每个 prompt 又触发一次 client.list_prompts()
        for server_name, info in self.registry.list_prompts():
            args = arguments_by_prompt.get(info.name)
            try:
                skill = await self.load_prompt_as_skill(
                    server_name=server_name,
                    prompt_name=info.name,
                    arguments=args,
                    prompt_info=info,
                )
                out.append(skill)
            except Exception as e:
                # 单个 prompt 失败不影响其它（与 refresh_tools / refresh_prompts
                # 的"单 server 失败隔离"语义一致）；但记录到 last_errors，
                # 让调用方能知道哪些失败（P1 修订）
                self.last_errors.append({
                    "server": server_name,
                    "prompt": info.name,
                    "error": f"{type(e).__name__}: {e}",
                })
                continue
        return out

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _lookup_prompt_info(
        self,
        client: MCPClient,
        server_name: str,
        prompt_name: str,
    ) -> MCPPromptInfo | None:
        """从 client.list_prompts() 找出指定 prompt 的 MCPPromptInfo。

        - prompts/list 失败：返回 None（让上层靠 prompts/get 自描述兜底）
        - prompt_name 不在列表中：返回 None（仍尝试 prompts/get；有些 server
          prompts/list 与 prompts/get 不一致）
        """
        try:
            infos = await client.list_prompts()
        except Exception:
            return None
        for info in infos:
            if info.name == prompt_name:
                return info
        return None


# ============================================================================
# 内部：把 MCPPromptResult.messages 拼成 PromptTemplate body
# ============================================================================


def _render_prompt_messages_to_body(
    *,
    server_name: str,
    prompt_name: str,
    messages: list[MCPPromptMessage],
) -> str:
    """把 prompt messages 拼成 Skill.prompt 文本。

    格式：

        [MCP Prompt: server/prompt]

        system: <content>

        user: <content>

        assistant: <content>

    system role 消息优先放在前面（便于 LLM 把它当 system 指令读）；
    其它 role 按原顺序。
    """
    header = f"[MCP Prompt: {server_name}/{prompt_name}]"
    if not messages:
        return header

    system_msgs = [m for m in messages if m.role == "system"]
    other_msgs = [m for m in messages if m.role != "system"]

    parts: list[str] = [header, ""]

    def _emit(msgs: list[MCPPromptMessage]) -> None:
        for m in msgs:
            role = m.role or "user"
            content = m.content.strip()
            parts.append(f"{role}: {content}" if content else f"{role}:")
            parts.append("")  # 空行分隔

    _emit(system_msgs)
    _emit(other_msgs)

    # 末尾去掉多余空行
    return "\n".join(parts).rstrip() + "\n"


__all__ = [
    "MCPPromptArgument",
    "MCPPromptInfo",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MAX_MCP_PROMPT_SKILL_NAME_LEN",
    "make_mcp_prompt_skill_name",
    "MCPPromptSkillAdapter",
]
