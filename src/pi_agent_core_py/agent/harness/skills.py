"""Harness skills and prompt templates.

Skill 把一组可复用的行为说明、系统提示片段、工具建议、metadata 组织起来。
PromptTemplate 负责把变量渲染成 prompt 文本。
SkillRegistry 负责注册 / 启用 / 禁用 / 选择 skill。
AgentHarness 负责在请求执行前把启用的 skills 注入 system_prompt。

```text
Agent        ：状态机 / queue / abort / event stream / messages（不感知 skill）
AgentHarness ：请求生命周期编排 + skill 注入（临时替换 agent.system_prompt）
Skill        ：name + description + prompt（str 或 PromptTemplate）+ metadata
SkillRegistry：register / unregister / enable / disable / select
```

设计要点：
- Skill 是**提示组织层**，不改变 Agent 执行内核
- Agent 不直接感知 Skill——Harness 临时渲染 system_prompt，请求结束还原
- Skill.tool_names 只作为提示和 metadata——**不自动注册工具**
- PromptTemplate 使用 Python `str.format()`——保持轻量，不引入 Jinja2
- Skill 信息进入 `context.metadata["skills"]` → 通过 Step 13 sync 进 snapshot / session

Step 14 **不做**：Compaction / Branch Summary / Vector Memory / 自动摘要。
"""
from __future__ import annotations

import typing
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# 异常
# ============================================================================


class PromptTemplateRenderError(Exception):
    """PromptTemplate 渲染失败。

    常见触发：缺少 variables 中声明的变量。
    """


class SkillRegistrationError(Exception):
    """Skill 注册失败。

    触发场景：
    - name 为空
    - description 为空
    - prompt 为空（None 或空字符串）
    - name 已注册（重复）
    """


class SkillNotFoundError(Exception):
    """按 name 查找 skill 但未找到。

    触发场景：
    - registry.get(name) 不存在
    - registry.enable(name) / disable(name) 不存在
    - registry.select(names=[name]) 含不存在的 name
    """


# ============================================================================
# PromptTemplate
# ============================================================================


class PromptTemplate(BaseModel):
    """轻量 prompt 模板——基于 Python `str.format()`。

    ```python
    PromptTemplate(
        name="finance_style",
        template="你是一个面向 {market} 的金融研究助手，输出风格：{style}",
        variables=["market", "style"],
    )

    template.render({"market": "A股", "style": "简洁"})
    # → "你是一个面向 A股 的金融研究助手，输出风格：简洁"
    ```

    不引入 Jinja2——保持依赖轻量。`variables` 是声明性字段，用于：
    - 渲染时校验是否缺变量
    - IDE / 文档展示
    - 后续可能的 schema 校验
    """
    name: str
    template: str
    variables: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def render(self, values: dict[str, Any] | None = None) -> str:
        """用 values 渲染模板。

        - values=None 等价于 {}
        - 缺 variables 中声明的变量 → PromptTemplateRenderError（含缺失变量名）
        - 多余的 values 字段被忽略（不报错）

        注意：`str.format(**values)` 会对 `{foo}` 这种字面花括号报错；当前实现假设
        template 不含未在 variables 中的占位符。若需要字面 `{` `}`，调用方应避免
        把它们写进 template（或后续 step 加 escape 机制）。
        """
        values = values or {}

        # 校验缺失变量
        missing = [v for v in self.variables if v not in values]
        if missing:
            raise PromptTemplateRenderError(
                f"PromptTemplate {self.name!r} 缺少变量：{missing}"
            )

        # str.format 对未在 variables 中的占位符也会 KeyError；我们只传 values 中
        # 与 variables 匹配的，避免误把多余 key 也喂进去
        try:
            relevant = {k: values[k] for k in self.variables}
            return self.template.format(**relevant)
        except KeyError as e:
            # 理论上 missing 校验已覆盖——保险起见
            raise PromptTemplateRenderError(
                f"PromptTemplate {self.name!r} 渲染时缺变量：{e.args[0]}"
            ) from e


# ============================================================================
# SkillStatus / Skill
# ============================================================================


#: Skill 启用 / 禁用状态。
#: - "enabled"  默认可被注入
#: - "disabled" 注册存在，但默认不注入
SkillStatus = Literal["enabled", "disabled"]


class Skill(BaseModel):
    """可复用的行为提示单元。

    字段：
      name         skill 唯一名称（必须非空）
      description  skill 描述（必须非空）——LLM / 调度系统靠它选 skill
      prompt       str 或 PromptTemplate——注入到 system_prompt 的内容
      status       enabled / disabled
      priority     注入顺序，数字越小越靠前；同 priority 按 name 升序
      tags         用于检索 / 筛选
      tool_names   skill 相关工具名（**仅 metadata**，不自动注册工具）
      metadata     skill 级 metadata（domain / version / author / ...）
    """
    name: str
    description: str
    prompt: str | PromptTemplate
    status: SkillStatus = "enabled"
    priority: int = 100
    tags: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def render_prompt(self, values: dict[str, Any] | None = None) -> str:
        """渲染 skill prompt。

        - prompt 是 str：直接返回（values 被忽略）
        - prompt 是 PromptTemplate：调 prompt.render(values)
        """
        if isinstance(self.prompt, str):
            return self.prompt
        return self.prompt.render(values)


# ============================================================================
# SkillRegistry
# ============================================================================


class SkillRegistry:
    """Skill 注册表——按 name 注册 / 查找 / 启用 / 禁用 / 选择。

    - register(skill)          注册；name 空 / description 空 / prompt 空 / 重复
                               → SkillRegistrationError
    - unregister(name)         注销；不存在则静默（与 ToolRegistry 一致）
    - get(name)                取 skill；不存在抛 SkillNotFoundError
    - has(name)                bool 检查（不抛错）
    - list()                   所有 skills（按插入序）
    - names()                  所有 skill name（按插入序）
    - enabled() / disabled()   按 status 过滤
    - enable(name) / disable(name) 切换 status；不存在抛 SkillNotFoundError
    - select(...)              按条件选择 skills 并排序
    """

    def __init__(self, skills: Iterable[Skill] | None = None):
        self._skills: dict[str, Skill] = {}
        for s in skills or []:
            self.register(s)

    # ------------------------------------------------------------------
    # 注册 / 注销 / 查找
    # ------------------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """注册 skill。

        抛 SkillRegistrationError：
        - name 为空
        - description 为空
        - prompt 为空（None 或 ""）
        - name 已注册（重复）
        """
        if not skill.name:
            raise SkillRegistrationError(f"{type(skill).__name__} 未设置 name")
        if not skill.description:
            raise SkillRegistrationError(
                f"Skill {skill.name!r} 未设置 description（LLM / 调度需要描述来选 skill）"
            )
        if isinstance(skill.prompt, str) and not skill.prompt:
            raise SkillRegistrationError(
                f"Skill {skill.name!r} 的 prompt 为空字符串"
            )
        if skill.name in self._skills:
            raise SkillRegistrationError(
                f"Skill {skill.name!r} 已注册（重复）"
            )
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        """注销 skill；不存在静默（不抛错）。"""
        self._skills.pop(name, None)

    def get(self, name: str) -> Skill:
        """取 skill；不存在抛 SkillNotFoundError。"""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(f"Skill {name!r} 未注册")
        return skill

    def has(self, name: str) -> bool:
        return name in self._skills

    def list(self) -> list[Skill]:
        """所有 skills（按插入序）。"""
        return list(self._skills.values())

    def names(self) -> list[str]:  # type: ignore[valid-type]
        """所有 skill name（按插入序）。"""
        return list(self._skills.keys())

    def enabled(self) -> list[Skill]:  # type: ignore[valid-type]
        """status == 'enabled' 的 skills（按插入序）。"""
        return [s for s in self._skills.values() if s.status == "enabled"]

    def disabled(self) -> list[Skill]:  # type: ignore[valid-type]
        """status == 'disabled' 的 skills（按插入序）。"""
        return [s for s in self._skills.values() if s.status == "disabled"]

    # ------------------------------------------------------------------
    # enable / disable
    # ------------------------------------------------------------------

    def enable(self, name: str) -> None:
        """启用 skill；不存在抛 SkillNotFoundError。"""
        self.get(name).status = "enabled"  # get 自身会抛 SkillNotFoundError

    def disable(self, name: str) -> None:
        """禁用 skill；不存在抛 SkillNotFoundError。"""
        self.get(name).status = "disabled"

    # ------------------------------------------------------------------
    # select
    # ------------------------------------------------------------------

    def select(
        self,
        *,
        names: list[str] | None = None,  # type: ignore[valid-type]
        tags: list[str] | None = None,  # type: ignore[valid-type]
        enabled_only: bool = True,
    ) -> list[Skill]:  # type: ignore[valid-type]
        """按条件选出 skills，最终按 (priority, name) 升序排序。

        选择规则（交集式）：
        1. 若 names 不为空：候选 = 包含在 names 中的 skill；不存在 names 元素 → SkillNotFoundError
        2. 若 tags 不为空：候选 = 含 tags 中任意 tag 的 skill
        3. 若 names 和 tags 都为空：候选 = 所有 skills
        4. names 与 tags 都传：先按 names 过滤，再按 tags 过滤（AND）

        若 enabled_only=True：进一步过滤掉 disabled。

        排序：priority 升序，同 priority 按 name 升序。
        """
        # Step 1: names 过滤
        if names is not None:
            # 显式列出 names 元素时，若不存在 → SkillNotFoundError
            for n in names:
                _ = self.get(n)  # 抛 SkillNotFoundError
            candidates = [self._skills[n] for n in names]
        else:
            candidates = list(self._skills.values())

        # Step 2: tags 过滤
        if tags is not None:
            tag_set = set(tags)
            candidates = [s for s in candidates if tag_set & set(s.tags)]

        # Step 3: enabled_only 过滤
        if enabled_only:
            candidates = [s for s in candidates if s.status == "enabled"]

        # Step 4: 排序——(priority, name)
        return sorted(candidates, key=lambda s: (s.priority, s.name))

    # ------------------------------------------------------------------
    # dunder
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self) -> typing.Iterator[Skill]:
        return iter(self._skills.values())


# ============================================================================
# SkillSelection / SkillInjectionConfig
# ============================================================================


class SkillSelection(BaseModel):
    """单次请求级的 skill 选择 + 渲染变量。

    字段：
      names   指定启用哪些 skill（按 registry 中 priority 排序）
      tags    按 tag 选择 skill
      values  PromptTemplate 渲染变量

    用于 AgentHarness.run_prompt / run_continue 的 skill_selection 入参。
    """
    names: list[str] | None = None
    tags: list[str] | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class SkillInjectionConfig(BaseModel):
    """Skill 注入到 system_prompt 的格式控制。

    字段：
      enabled                是否启用 skill 注入（False 时 render_system_prompt 直接返回 base）
      section_title          注入 section 的标题（默认 "## Skills"）
      include_descriptions   是否输出 "Description:" 行
      include_tool_names     是否输出 "Tools:" 行
      separator              多个 skill 之间的分隔符（默认双换行）
    """
    enabled: bool = True
    section_title: str = "Skills"
    include_descriptions: bool = True
    include_tool_names: bool = True
    separator: str = "\n\n"


# ============================================================================
# render_skill_block
# ============================================================================


def render_skill_block(
    skills: list[Skill],
    *,
    values: dict[str, Any] | None = None,
    config: SkillInjectionConfig | None = None,
) -> str:
    """把 skills 渲染成可注入到 system_prompt 的文本块。

    格式（include_descriptions=True, include_tool_names=True 时）：

    ```text
    ## Skills

    ### skill_a
    Description: ...
    Tools: tool_x, tool_y

    <rendered skill prompt>

    ### skill_b
    Description: ...

    <rendered skill prompt>
    ```

    - 若 skills 为空：返回空字符串
    - include_descriptions=False：省略 Description 行
    - include_tool_names=False 或 skill.tool_names 为空：省略 Tools 行
    - separator 控制多个 skill prompt 之间的分隔
    """
    if not skills:
        return ""
    cfg = config or SkillInjectionConfig()
    values = values or {}

    sections: list[str] = [f"## {cfg.section_title}"]

    for skill in skills:
        lines: list[str] = [f"### {skill.name}"]
        if cfg.include_descriptions:
            lines.append(f"Description: {skill.description}")
        if cfg.include_tool_names and skill.tool_names:
            lines.append(f"Tools: {', '.join(skill.tool_names)}")
        # 渲染 prompt（str 或 PromptTemplate）
        rendered = skill.render_prompt(values)
        # 用空行分隔 header 和 prompt body
        body = "\n".join(lines)
        sections.append(f"{body}\n\n{rendered}")

    return cfg.separator.join(sections)


__all__ = [
    # 异常
    "PromptTemplateRenderError",
    "SkillRegistrationError", "SkillNotFoundError",
    # PromptTemplate
    "PromptTemplate",
    # Skill
    "SkillStatus", "Skill",
    # Registry
    "SkillRegistry",
    # Selection / Config
    "SkillSelection", "SkillInjectionConfig",
    # Render
    "render_skill_block",
]
