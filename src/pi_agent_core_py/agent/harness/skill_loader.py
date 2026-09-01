"""Secure filesystem loader for harness skills.

把本地 `SKILL.md` 文件加载成项目内部的 `Skill`，与手写 Skill / MCP prompt
Skill 共同注册进 `SkillRegistry`。

```text
skills/my-skill/SKILL.md
↓
SkillFileLoader.load_file / load_dir / load_many
↓
parse_skill_markdown
↓
Skill(metadata={"source": "file", "path": ..., "loader": "SkillFileLoader"})
↓
SkillRegistry
↓
AgentHarness.attach_skills / attach_skill_files / attach_skill_dir
↓
render_system_prompt
```

设计要点：

- **只读 markdown，不执行任何代码**——SKILL.md 中提到的 Python 文件不被
  import；RAG / vector store / 知识库检索不在本 step 范围。
- **路径沙箱**：可选 `root_dirs` 限制；用 `Path.resolve()` + `is_path_within_roots`
  防 `../` 逃逸。
- **大小限制**：默认 256 KB；超过抛 `SkillFileSecurityError`。
- **文件名 allowlist**：默认只识别 `SKILL.md` / `skill.md`，避免误读 README。
- **frontmatter**：可选；用 `yaml.safe_load`（pyyaml 已经在 pyproject 依赖中）。
- **重名处理**：load_dir / load_many 把多个 Skill 返回给调用方，重名由
  SkillRegistry.register 拒绝（保持 Step 14 既有契约）。

不实现：

- 文件监听热加载（不在本副本）
- 跨项目共享 skill 库（不在本副本）
- 远程 marketplace（不在 roadmap）
- RAG / vector / long-term memory（显式排除）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ...policy import is_path_within_roots
from .skills import PromptTemplate, Skill

# ============================================================================
# 异常
# ============================================================================


class SkillFileLoadError(Exception):
    """Skill 文件加载失败的基类。"""


class SkillFileFormatError(SkillFileLoadError):
    """文件格式错误：

    - 非 markdown 文件
    - frontmatter 不是合法 YAML
    - frontmatter 字段类型错误
    - markdown 中无法推断 name 且未提供 fallback
    """


class SkillFileSecurityError(SkillFileLoadError):
    """安全策略拒绝加载：

    - 文件大小超过 max_file_size_bytes
    - 路径逃逸出 root_dirs
    - 路径含非法字符（如 NUL）
    """


# ============================================================================
# SkillLoadConfig
# ============================================================================


#: 默认识别的 markdown 文件名。Step 19 收紧到 SKILL.md / skill.md——
#: 避免递归扫描时误把 README.md / CHANGELOG.md 等当成 skill。
DEFAULT_ALLOWED_FILENAMES: frozenset[str] = frozenset({"SKILL.md", "skill.md"})

#: 默认最大文件大小：256 KB。SKILL.md 应该是行为说明，不是知识库——
#: 超过这个尺寸基本可以断定被误用。
DEFAULT_MAX_FILE_SIZE_BYTES: int = 256_000


class SkillLoadConfig(BaseModel):
    """SkillFileLoader 的配置。

    字段：
        root_dirs            限制 load_file / load_dir 只能读这些根目录下的文件；
                             空列表 = 不限制（仍受 max_file_size_bytes / allowed_filenames 约束）
        allow_frontmatter    是否解析 YAML frontmatter；False 时按纯 markdown 推断
        default_enabled      frontmatter 没写 enabled 时的默认值
        default_priority     frontmatter 没写 priority 时的默认值
        max_file_size_bytes  单文件大小上限；超过抛 SkillFileSecurityError
        allowed_filenames    load_dir 时识别的文件名集合（小写比较由调用方处理）
    """
    root_dirs: list[str] = Field(default_factory=list)
    allow_frontmatter: bool = True
    default_enabled: bool = True
    default_priority: int = 0
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    allowed_filenames: set[str] = Field(
        default_factory=lambda: set(DEFAULT_ALLOWED_FILENAMES)
    )


# ============================================================================
# Frontmatter / Markdown 解析
# ============================================================================


#: frontmatter 起始 / 结束标记
_FM_OPEN = re.compile(r"^---\s*$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """把 markdown 拆成 (frontmatter dict, body)。

    若文本不以 `---\\n` 起始，返回 (None, text)。
    frontmatter YAML 解析失败时抛 SkillFileFormatError。
    """
    if not text.startswith("---"):
        return None, text

    # 找下一个单独一行的 `---`
    # 跳过第一行（开头的 ---）
    after_first = text.split("\n", 1)
    if len(after_first) < 2:
        # 整个文件就一行 `---`，没有 body
        return None, text

    rest = after_first[1]
    # 在 rest 中找结束的 `---`
    m = _FM_OPEN.search(rest)
    if m is None:
        # 没有结束标记 → 整个文件当成无 frontmatter
        return None, text

    fm_text = rest[: m.start()]
    body = rest[m.end():]
    # 去掉 body 起始的换行
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]

    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise SkillFileFormatError(
            f"frontmatter is not valid YAML: {e}"
        ) from e

    if loaded is None:
        return {}, body
    if not isinstance(loaded, dict):
        raise SkillFileFormatError(
            f"frontmatter must be a YAML mapping, got {type(loaded).__name__}"
        )
    return loaded, body


#: 一级标题正则——匹配 `# Title` 行（前后允许空格；title 不能跨行）
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
#: 二级标题正则——匹配 `## Section`
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _find_section_body(body: str, section_title: str) -> str | None:
    """提取 markdown 中 `## section_title` 到下一个同级或更高级标题之间的内容。

    找不到该 section → None。
    section_title 比较忽略大小写与首尾空格。
    """
    matches = list(_H2_RE.finditer(body))
    target = section_title.strip().lower()
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == target:
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return body[start:end].strip()
    return None


def _infer_name_from_h1(body: str) -> str | None:
    """从 body 的第一个一级标题取 title（去掉首尾空白）。

    没有一级标题 → None。
    """
    m = _H1_RE.search(body)
    if m is None:
        return None
    return m.group(1).strip()


def _normalize_tags(raw: Any) -> list[str]:
    """frontmatter.tags 可以是 list[str] 或逗号分隔 str；统一成 list[str]。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        return [p for p in parts if p]
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                v = item.strip()
                if v:
                    out.append(v)
            else:
                # 非字符串 tag 强制 str（防御坏 frontmatter）
                out.append(str(item))
        return out
    # 其它类型（dict / int / ...）——转单元素 str
    return [str(raw)]


#: 布尔字面量允许的字符串形式。YAML frontmatter 中 bool 字段如果忘了去掉
#: 引号（如 `enabled: "false"`），yaml.safe_load 会得到 str 而非 bool——
#: 直接 bool(str) 会把任何非空字符串当 True，这是常见陷阱。本表用于显式
#: 把字符串映射到 bool，未匹配的字符串抛 SkillFileFormatError。
_TRUE_LITERALS: frozenset[str] = frozenset({"true", "yes", "1", "on"})
_FALSE_LITERALS: frozenset[str] = frozenset({"false", "no", "0", "off"})


def _normalize_bool(raw: Any, *, default: bool, field_name: str) -> bool:
    """把 frontmatter 中应该为 bool 的字段统一成 bool。

    - raw is None → 返回 default
    - raw is bool → 直接返回
    - raw is str → 大小写不敏感匹配 true/yes/1/on 或 false/no/0/off；
      未匹配抛 SkillFileFormatError
    - 其它类型（int / list / dict）→ 抛 SkillFileFormatError（避免静默错配）

    field_name 仅用于错误信息。
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in _TRUE_LITERALS:
            return True
        if v in _FALSE_LITERALS:
            return False
        raise SkillFileFormatError(
            f"frontmatter.{field_name} must be bool or recognized string "
            f"(true/false/yes/no/1/0/on/off), got {raw!r}"
        )
    raise SkillFileFormatError(
        f"frontmatter.{field_name} must be bool, got {type(raw).__name__}: {raw!r}"
    )


def parse_skill_markdown(
    text: str,
    *,
    fallback_name: str,
    source_path: str | None = None,
    config: SkillLoadConfig | None = None,
) -> Skill:
    """把 SKILL.md 文本解析成 Skill。

    解析规则（与文档一致）：

        name         优先级：frontmatter.name → body 第一行 H1 → fallback_name
        description  优先级：frontmatter.description → body ## Description 第一段
                                          → body 第一行非 H1 文本 → 空字符串
        instructions 优先级：## Instructions 下内容 → body 全文（去掉 H1 / frontmatter）
        tags         frontmatter.tags（list 或逗号分隔 str）
        enabled      frontmatter.enabled（默认 config.default_enabled）
        priority     frontmatter.priority（默认 config.default_priority）
        tool_names   frontmatter.tool_names（list）

    参数：
        text          SKILL.md 文件内容
        fallback_name frontmatter / H1 都没有时的兜底 name（通常是文件夹名）
        source_path   源文件路径；写入 metadata["path"]
        config        控制 allow_frontmatter / default_enabled / default_priority

    抛 SkillFileFormatError：frontmatter YAML 不合法 / 类型错误 / name 推断失败。
    """
    cfg = config or SkillLoadConfig()
    if not fallback_name or not fallback_name.strip():
        raise SkillFileFormatError(
            "parse_skill_markdown: fallback_name must be non-empty"
        )

    fm: dict[str, Any] | None = None
    body = text
    if cfg.allow_frontmatter:
        fm, body = _split_frontmatter(text)
        if fm is None:
            fm = {}
    else:
        fm = {}

    # name
    name_raw = fm.get("name")
    if isinstance(name_raw, str) and name_raw.strip():
        name = name_raw.strip()
    else:
        h1_name = _infer_name_from_h1(body)
        if h1_name:
            name = h1_name
        else:
            name = fallback_name.strip()

    # description
    description_raw = fm.get("description")
    if isinstance(description_raw, str) and description_raw.strip():
        description = description_raw.strip()
    else:
        desc_section = _find_section_body(body, "Description")
        if desc_section:
            description = desc_section.strip()
        else:
            description = ""

    # instructions（用于 PromptTemplate body）
    instructions_section = _find_section_body(body, "Instructions")
    if instructions_section is not None:
        instructions_body = instructions_section
    else:
        # 没有 ## Instructions：用整份 body（已经去掉 frontmatter）
        # 去掉首尾空白即可
        instructions_body = body.strip()

    # tags / enabled / priority / tool_names
    tags = _normalize_tags(fm.get("tags"))

    enabled_raw = fm.get("enabled")
    enabled = _normalize_bool(
        enabled_raw, default=cfg.default_enabled, field_name="enabled",
    )

    priority_raw = fm.get("priority")
    if priority_raw is None:
        priority = cfg.default_priority
    else:
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError) as e:
            raise SkillFileFormatError(
                f"frontmatter.priority must be int, got {priority_raw!r}"
            ) from e

    tool_names_raw = fm.get("tool_names") or fm.get("tools") or []
    if isinstance(tool_names_raw, str):
        tool_names = [t.strip() for t in tool_names_raw.split(",") if t.strip()]
    elif isinstance(tool_names_raw, (list, tuple)):
        tool_names = [str(t).strip() for t in tool_names_raw if str(t).strip()]
    else:
        tool_names = []

    # 组装 Skill——name / description 由 SkillRegistry.register 在
    # 真正注册时再做非空校验（与 Step 14 既有契约一致）
    metadata: dict[str, Any] = {
        "source": "file",
        "loader": "SkillFileLoader",
    }
    if source_path:
        metadata["path"] = str(source_path)

    return Skill(
        name=name,
        description=description,
        prompt=PromptTemplate(
            name=name,
            template=instructions_body,
            variables=[],
        ),
        status="enabled" if enabled else "disabled",
        priority=priority,
        tags=tags,
        tool_names=tool_names,
        metadata=metadata,
    )


# ============================================================================
# SkillFileLoader
# ============================================================================


class SkillFileLoader:
    """SKILL.md → Skill 加载器。

    用法：

        loader = SkillFileLoader(SkillLoadConfig(root_dirs=["./skills"]))
        skill = loader.load_file("./skills/foo/SKILL.md")
        skills = loader.load_dir("./skills", recursive=True)

    生命周期无状态——构造后可反复调用 load_*。
    """

    def __init__(self, config: SkillLoadConfig | None = None) -> None:
        self.config: SkillLoadConfig = config or SkillLoadConfig()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def load_file(self, path: str | Path) -> Skill:
        """读取并解析单个 SKILL.md。

        - 路径校验：root_dirs 配置时必须在 roots 内
        - 文件名校验：必须在 allowed_filenames 内（小写比较）
        - 大小校验：不超过 max_file_size_bytes
        - 解析失败：SkillFileFormatError
        - 安全失败：SkillFileSecurityError
        """
        p = self._validate_path(path)
        self._check_filename(p)
        self._check_size(p)

        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise SkillFileFormatError(
                f"failed to decode {p} as utf-8: {e}"
            ) from e

        fallback_name = self._derive_fallback_name(p)
        return parse_skill_markdown(
            text,
            fallback_name=fallback_name,
            source_path=str(p),
            config=self.config,
        )

    def load_dir(
        self,
        root: str | Path,
        *,
        recursive: bool = True,
    ) -> list[Skill]:
        """扫描目录下的 SKILL.md，按文件名排序逐个加载。

        - recursive=True：递归扫描所有子目录
        - recursive=False：只扫一层
        - 单个文件加载失败（format / security）**抛出并停止**——保持 fail-fast
          语义；调用方若想容错，应自己 enumerate + try/except
        - **root_dirs 仍生效**：load_dir 找到的每个 SKILL.md 都会走 load_file，
          因此每个文件路径仍必须落在 root_dirs 内。若 root 配置在 root_dirs 外，
          应自己先把 root 加进 root_dirs，或不要传 root_dirs 配置。
        """
        root_path = Path(root).resolve()
        if not root_path.exists():
            raise SkillFileLoadError(f"load_dir: root does not exist: {root_path}")
        if not root_path.is_dir():
            raise SkillFileLoadError(
                f"load_dir: root is not a directory: {root_path}"
            )

        # 找出所有匹配 allowed_filenames 的文件
        allowed_lower = {fn.lower() for fn in self.config.allowed_filenames}
        matches: list[Path] = []
        if recursive:
            for p in root_path.rglob("*"):
                if p.is_file() and p.name.lower() in allowed_lower:
                    matches.append(p)
        else:
            for p in root_path.iterdir():
                if p.is_file() and p.name.lower() in allowed_lower:
                    matches.append(p)

        # 按路径排序——保证多次加载结果稳定
        matches.sort(key=lambda x: str(x).lower())

        return [
            self.load_file(p)
            for p in matches
        ]

    def load_many(self, paths: list[str | Path]) -> list[Skill]:
        """批量加载多个文件。

        单个失败立即抛出（与 load_dir 一致）。返回的列表顺序与输入一致。
        """
        return [self.load_file(p) for p in paths]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _validate_path(self, path: str | Path) -> Path:
        """校验路径：root_dirs 限制 + 解析成绝对路径。

        - root_dirs 为空：直接 resolve 返回
        - root_dirs 非空：路径必须在任一 root 内（用 is_path_within_roots）
        - 任何 OSError（不存在 / 权限）转 SkillFileLoadError
        """
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, ValueError) as e:
            raise SkillFileLoadError(
                f"invalid path {path!r}: {e}"
            ) from e

        if self.config.root_dirs:
            if not is_path_within_roots(str(resolved), self.config.root_dirs):
                raise SkillFileSecurityError(
                    f"path {resolved} is outside configured root_dirs"
                )
        return resolved

    def _check_filename(self, p: Path) -> None:
        allowed_lower = {fn.lower() for fn in self.config.allowed_filenames}
        if p.name.lower() not in allowed_lower:
            raise SkillFileFormatError(
                f"file {p.name!r} is not in allowed_filenames={sorted(allowed_lower)}"
            )

    def _check_size(self, p: Path) -> None:
        try:
            size = p.stat().st_size
        except OSError as e:
            raise SkillFileLoadError(
                f"failed to stat {p}: {e}"
            ) from e
        if size > self.config.max_file_size_bytes:
            raise SkillFileSecurityError(
                f"file {p} size {size} exceeds max_file_size_bytes="
                f"{self.config.max_file_size_bytes}"
            )

    @staticmethod
    def _derive_fallback_name(p: Path) -> str:
        """用父目录名作 fallback_name（与「一个 skill 一个文件夹」约定一致）。

        - 父目录名是 `.`（load_file 直接传当前目录下文件）→ 用文件 stem
        - 父目录名为空 → 用文件 stem
        """
        parent_name = p.parent.name
        if parent_name and parent_name not in (".", "..", ""):
            return parent_name
        return p.stem


__all__ = [
    # 异常
    "SkillFileLoadError",
    "SkillFileFormatError",
    "SkillFileSecurityError",
    # 配置
    "SkillLoadConfig",
    "DEFAULT_ALLOWED_FILENAMES",
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    # 解析 / 加载
    "parse_skill_markdown",
    "SkillFileLoader",
]
