"""默认对话向 system prompt（P0-5）。

当 `Agent.system_prompt` 为空（或仅 whitespace）时，Harness 自动用本模块
构造一份「对话助手」风格 prompt，让模型默认像 claude.ai / chatgpt 一样
回答，而不是 coding agent。

设计要点：
- 纯函数 `build_default_system_prompt(...)`，无 I/O / 无副作用
- 模板内容（按 user spec）：
    1. 友好 / 专业 / 直接
    2. 用用户输入语言回答
    3. 不知道承认不知道
    4. 不编造
    5. 会话文件夹用 list_files / view_file / write_file
    6. 有图片时直读；不支持时告知
    7. 调用工具前简短说明意图
    8. 工具结果融入回答
    9. Skills 按 priority 排序注入
    10. MCP 工具按 server/tool 分组注入
- 模板分段拼接，skills/mcp 段缺失时省略，避免空段落
- 与 Harness 现有 skill_injection_config 解耦——本模块只负责基础对话向 prompt；
  skill_registry 的「选中 + 渲染 skill block」逻辑仍由 harness._prepare_skill_prompt
  负责（在本模块生成的 base 之上叠加）
"""
from __future__ import annotations

from typing import Any

from .skills import Skill

# ============================================================================
# 文本片段
# ============================================================================


_BASE_PROMPT = """你是一个友好、专业、直接的对话助手。

行为准则：
- 用与用户输入相同的语言回答（中文输入用中文，英文输入用英文）
- 直接回答用户问题，不绕弯
- 不知道就承认不知道，不要编造事实
- 调用工具前简短说明意图（例如"我先查看一下你上传的文件"）
- 工具返回的结果要融入回答中，不要原样 dump
- 不要假装已经读取未调用工具的文件内容；需要时主动调用 view_file
- 用户要求生成报告或其它文本交付物时，用 write_file 保存到 artifacts/**；共享笔记放 docs/notes/**
- 编程任务使用 coding_* 工具在 Sandbox 中修改 scripts/**，验证并冻结后等待用户批准

当前对话绑定一个独立的会话文件夹：
- 根目录 AGENT.md 包含当前会话的用户指令，并会在每轮请求中自动加载
- 每个成功完成的普通会话轮次都会自动提炼并累计更新根目录 Memory.md；后续请求自动加载该记忆
- /checkpointer 用于显式总结当前完整对话并在成功后清空聊天消息，不是自动记忆的必需步骤
- 用户上传的普通文件位于只读 inputs/**，上传代码位于 scripts/**；固定文档位于 documents/**
- 你通过 write_file 创建的非代码产物位于 artifacts/**
  不要改写 HANDOFF.md、tasks/** 或固定 docs 摘要
- 支持读取的格式：markdown、html、csv、parquet、常见文本/代码文件
  （txt / json / yaml / xml / toml / py / ts / js / sql / 等等）
- 先调 list_files 看有哪些文件，再用 view_file(file_id=...) 读取内容
  （默认前 64 KB 或前 50 行）
- 当前不支持图片内容分析（不做 OCR、不做视觉理解）。
  若用户上传图片或问题依赖图片内容，请明确告知"当前不支持图片"
- PDF / 二进制 / 未知格式：仅返回元信息，不强行解析"""


_FILE_TOOLS_HINT = """

可用工具（始终启用，当 file_store 已配置时）：
- list_files：列出当前会话文件夹内的文件（id / name / mime / size / format）
- view_file：读取文件内容或结构摘要
    支持 markdown / html / csv / parquet / 常见文本/代码文件
    图片明确返回 unsupported；PDF / 二进制仅返回元信息
- write_file：创建新的 UTF-8 文本交付物
    非代码默认进入 artifacts/**，代码进入 scripts/**，也可写 docs/notes/**；不接受物理路径且不会覆盖
- web_search：搜索网页（如已注册）"""


_KNOWLEDGE_HINT = """

知识库工具（当知识库已启用时）：
- search_knowledge：搜索当前会话有权限访问的知识库
    返回结构化的 Evidence（[E1] / [E2] / ...），含来源文件名、页码、标题路径、内容片段

引用知识库内容时：
- 使用工具返回的 Evidence ID，例如 [cite:E1]
- 不要编造文件名、页码或 Evidence ID
- 引用多个来源时可以叠加：[cite:E1][cite:E2]
- 服务器会自动将 [cite:E1] 转换为 [1] 并在回答末尾附上来源列表"""


# ============================================================================
# 渲染辅助
# ============================================================================


def _format_skills_section(skills: list[Skill]) -> str:
    """按 (priority, name) 升序渲染 skills 段。

    格式：
        ## 可用 Skills（按 priority 排序）

        ### {name}  · priority={priority} tags={tags}
        {description}
    """
    sorted_skills = sorted(skills, key=lambda s: (s.priority, s.name))
    lines = ["## 可用 Skills（按 priority 排序）", ""]
    for s in sorted_skills:
        tags_str = ", ".join(s.tags) if s.tags else "—"
        lines.append(
            f"### {s.name}  · priority={s.priority} tags=[{tags_str}]"
        )
        lines.append(s.description)
        if s.tool_names:
            lines.append(f"相关工具：{', '.join(s.tool_names)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_mcp_tools_section(mcp_tools: list[Any]) -> str:
    """按 server 分组渲染 MCP tools 段。

    入参期望是 `MCPAgentTool` 列表（含 .server_name / .name / .description）。
    缺字段时降级为字符串化。

    格式：
        ## 可用 MCP 工具

        ### server: {server_name}
        - {tool_name}：{description}
        ...
    """
    by_server: dict[str, list[Any]] = {}
    for t in mcp_tools:
        server = getattr(t, "server_name", None) or "unknown"
        by_server.setdefault(server, []).append(t)

    lines = ["## 可用 MCP 工具", ""]
    for server in sorted(by_server.keys()):
        tools = by_server[server]
        lines.append(f"### server: {server}")
        for t in sorted(tools, key=lambda x: getattr(x, "name", "")):
            name = getattr(t, "name", str(t))
            desc = getattr(t, "description", "") or ""
            # description 可能多行——首行展开，余行缩进
            desc_first = desc.split("\n", 1)[0].strip()
            lines.append(f"- {name}：{desc_first}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ============================================================================
# 入口：build_default_system_prompt
# ============================================================================


def build_default_system_prompt(
    *,
    skills: list[Skill] | None = None,
    mcp_tools: list[Any] | None = None,
    file_tools_enabled: bool = True,
    knowledge_enabled: bool = False,
) -> str:
    """构造默认对话向 system prompt。

    参数：
        skills: 启用的 skills 列表（按 priority 注入；None / 空则省略 skills 段）
        mcp_tools: 启用的 MCP 工具列表（MCPAgentTool 或 duck-typed 对象，
                   含 server_name / name / description）
        file_tools_enabled: 是否在 prompt 中提及 list_files / view_file /
                            write_file / web_search
                            （P0-3 完成前为 True 也无副作用——LLM 收到不存在的
                            工具调用会失败，prompt 仅作上下文说明）
        knowledge_enabled: 是否在 prompt 中提及 search_knowledge + [cite:E1] 引用规则
                           （P2-R4-C2：当 search_knowledge 工具已注册时设为 True）

    返回：拼好的 system_prompt 字符串（非空）

    纯函数：不读文件 / 不调 LLM / 不查注册表——所有内容来自入参。
    """
    sections: list[str] = [_BASE_PROMPT]

    if file_tools_enabled:
        sections.append(_FILE_TOOLS_HINT)

    if knowledge_enabled:
        sections.append(_KNOWLEDGE_HINT)

    if skills:
        sections.append(_format_skills_section(skills))

    if mcp_tools:
        sections.append(_format_mcp_tools_section(mcp_tools))

    return "\n\n".join(sections)


__all__ = ["build_default_system_prompt", "_KNOWLEDGE_HINT"]
