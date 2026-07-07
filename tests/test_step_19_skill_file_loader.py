"""Step 19 — SkillFileLoader 单元测试。

覆盖：
- parse_skill_markdown：frontmatter / H1 / Description / Instructions / tags / enabled / priority
- SkillFileLoader.load_file：基础读取、目录名作 fallback、大小限制、文件名 allowlist
- SkillFileLoader.load_dir：recursive / 非递归 / 单个失败 fail-fast
- SkillFileLoader.load_many：批量
- 安全：root_dirs 限制 / `..` 逃逸 / 非 markdown 文件 / 超大文件
- 错误：frontmatter YAML 不合法 / 文件名不在 allowlist / 缺 fallback name
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py import (
    DEFAULT_MAX_FILE_SIZE_BYTES,
    Skill,
    SkillFileFormatError,
    SkillFileLoader,
    SkillFileLoadError,
    SkillFileSecurityError,
    SkillLoadConfig,
    parse_skill_markdown,
)

# ============================================================================
# parse_skill_markdown
# ============================================================================


def test_parse_no_frontmatter_uses_h1_as_name() -> None:
    text = """# Code Review

## Description

A code review skill.

## Instructions

- Read the diff
- Check style
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert skill.name == "Code Review"
    assert skill.description == "A code review skill."
    assert "Read the diff" in skill.prompt.template
    assert skill.metadata["source"] == "file"
    assert skill.metadata["loader"] == "SkillFileLoader"


def test_parse_no_frontmatter_no_h1_uses_fallback() -> None:
    text = """## Description

desc here

## Instructions

do X
"""
    skill = parse_skill_markdown(text, fallback_name="fallback-name")
    assert skill.name == "fallback-name"
    assert skill.description == "desc here"


def test_parse_frontmatter_overrides_h1() -> None:
    text = """---
name: my-skill
description: from-fm
priority: 10
enabled: false
tags:
  - python
  - debug
tool_names:
  - read_file
---

# Some Title

## Instructions

body
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert skill.name == "my-skill"
    assert skill.description == "from-fm"
    assert skill.priority == 10
    assert skill.status == "disabled"
    assert skill.tags == ["python", "debug"]
    assert skill.tool_names == ["read_file"]


def test_parse_frontmatter_tags_as_csv_string() -> None:
    text = """---
name: x
description: y
tags: python, debug,  test
---

# X
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert skill.tags == ["python", "debug", "test"]


def test_parse_frontmatter_invalid_yaml_raises_format_error() -> None:
    text = """---
name: [unclosed
---

body
"""
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown(text, fallback_name="dir")


def test_parse_frontmatter_not_a_mapping_raises() -> None:
    text = """---
- item1
- item2
---

body
"""
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown(text, fallback_name="dir")


def test_parse_frontmatter_priority_must_be_int() -> None:
    text = """---
name: x
priority: not-a-number
---

body
"""
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown(text, fallback_name="dir")


def test_parse_frontmatter_enabled_string_false_is_disabled() -> None:
    """P1 修订：enabled: "false"（字符串）应该解析为 False。

    旧逻辑 bool("false") == True 是常见陷阱；现在用 _normalize_bool。
    """
    text = """---
name: x
description: d
enabled: "false"
---

body
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert skill.status == "disabled"


def test_parse_frontmatter_enabled_string_true_is_enabled() -> None:
    text = """---
name: x
description: d
enabled: "true"
---

body
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert skill.status == "enabled"


def test_parse_frontmatter_enabled_recognizes_yes_no_on_off() -> None:
    for raw, expected in [("yes", True), ("no", False), ("on", True), ("off", False),
                          ("1", True), ("0", False), ("TRUE", True), ("FALSE", False)]:
        text = f"""---
name: x
description: d
enabled: {raw!r}
---

body
"""
        skill = parse_skill_markdown(text, fallback_name="dir")
        assert skill.status == ("enabled" if expected else "disabled"), raw


def test_parse_frontmatter_enabled_unknown_string_raises() -> None:
    text = """---
name: x
description: d
enabled: maybe
---

body
"""
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown(text, fallback_name="dir")


def test_parse_frontmatter_enabled_int_raises() -> None:
    """非 bool / 非 str（如 int）不应被静默接受。"""
    text = """---
name: x
description: d
enabled: 2
---

body
"""
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown(text, fallback_name="dir")


def test_parse_fallback_name_empty_raises() -> None:
    with pytest.raises(SkillFileFormatError):
        parse_skill_markdown("body", fallback_name="")


def test_parse_no_instructions_section_uses_full_body() -> None:
    text = """# Title

## Description

d

This is the body.
"""
    skill = parse_skill_markdown(text, fallback_name="dir")
    assert "This is the body." in skill.prompt.template


def test_parse_default_priority_enabled_via_config() -> None:
    text = "# H1\n## Description\nd\n## Instructions\ni\n"
    cfg = SkillLoadConfig(default_priority=42, default_enabled=False)
    skill = parse_skill_markdown(text, fallback_name="d", config=cfg)
    assert skill.priority == 42
    assert skill.status == "disabled"


def test_parse_allow_frontmatter_false_ignores_fm() -> None:
    text = """---
name: ignored
---

# Real Title
"""
    cfg = SkillLoadConfig(allow_frontmatter=False)
    skill = parse_skill_markdown(text, fallback_name="dir", config=cfg)
    assert skill.name == "Real Title"


def test_parse_includes_examples_section_inside_instructions_if_no_instructions() -> None:
    """没有 ## Instructions 时 body 全文作为 prompt——Examples 也包含在内。"""
    text = """# Title

## Description

d

## Examples

User: hi
Assistant: hello
"""
    skill = parse_skill_markdown(text, fallback_name="d")
    assert "Examples" in skill.prompt.template
    assert "hello" in skill.prompt.template


# ============================================================================
# SkillFileLoader.load_file
# ============================================================================


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_load_file_basic(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_md = skill_dir / "SKILL.md"
    _write(skill_md, "# My Skill\n## Description\nd\n## Instructions\nbody\n")

    loader = SkillFileLoader()
    skill = loader.load_file(skill_md)
    assert skill.name == "My Skill"
    assert skill.metadata["path"] == str(skill_md.resolve())


def test_load_file_uses_directory_name_as_fallback(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-cool-skill"
    skill_md = skill_dir / "SKILL.md"
    _write(skill_md, "## Description\nd\n## Instructions\nbody\n")

    loader = SkillFileLoader()
    skill = loader.load_file(skill_md)
    assert skill.name == "my-cool-skill"


def test_load_file_filename_not_in_allowlist_raises(tmp_path: Path) -> None:
    p = tmp_path / "README.md"
    _write(p, "# x\n")
    loader = SkillFileLoader()
    with pytest.raises(SkillFileFormatError):
        loader.load_file(p)


def test_load_file_size_exceeds_max_raises(tmp_path: Path) -> None:
    p = tmp_path / "skill" / "SKILL.md"
    _write(p, "# x\n## Description\nd\n## Instructions\n" + ("x" * 1000))
    cfg = SkillLoadConfig(max_file_size_bytes=100)
    loader = SkillFileLoader(cfg)
    with pytest.raises(SkillFileSecurityError):
        loader.load_file(p)


def test_load_file_outside_root_dirs_raises(tmp_path: Path) -> None:
    inside = tmp_path / "skills"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    skill_md = outside / "SKILL.md"
    _write(skill_md, "# x\n## Description\nd\n## Instructions\nb\n")
    cfg = SkillLoadConfig(root_dirs=[str(inside)])
    loader = SkillFileLoader(cfg)
    with pytest.raises(SkillFileSecurityError):
        loader.load_file(skill_md)


def test_load_file_within_root_dirs_succeeds(tmp_path: Path) -> None:
    inside = tmp_path / "skills"
    inside.mkdir()
    skill_md = inside / "SKILL.md"
    _write(skill_md, "# x\n## Description\nd\n## Instructions\nb\n")
    cfg = SkillLoadConfig(root_dirs=[str(inside)])
    loader = SkillFileLoader(cfg)
    skill = loader.load_file(skill_md)
    assert skill.name == "x"


def test_load_file_dotdot_escape_rejected(tmp_path: Path) -> None:
    """通过 .. 拼接逃逸出 root_dirs 应被拒绝。"""
    root = tmp_path / "skills"
    root.mkdir()
    secret = tmp_path / "secret" / "SKILL.md"
    _write(secret, "# s\n## Description\nd\n## Instructions\nsecret\n")
    # 用 ../secret/SKILL.md 访问 root 外
    escape_path = (root / ".." / "secret" / "SKILL.md")
    cfg = SkillLoadConfig(root_dirs=[str(root)])
    loader = SkillFileLoader(cfg)
    with pytest.raises(SkillFileSecurityError):
        loader.load_file(escape_path)


def test_load_file_unicode_decode_error_raises_format(tmp_path: Path) -> None:
    p = tmp_path / "skill" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe# invalid utf-8\n")
    loader = SkillFileLoader()
    with pytest.raises(SkillFileFormatError):
        loader.load_file(p)


# ============================================================================
# SkillFileLoader.load_dir / load_many
# ============================================================================


def test_load_dir_recursive_finds_nested_skills(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    _write(tmp_path / "b" / "sub" / "skill.md", "# B\n## Description\ndb\n## Instructions\nib\n")
    loader = SkillFileLoader()
    skills = loader.load_dir(tmp_path, recursive=True)
    names = sorted(s.name for s in skills)
    assert names == ["A", "B"]


def test_load_dir_non_recursive_only_top_level(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", "# Top\n## Description\nd\n## Instructions\ni\n")
    _write(tmp_path / "sub" / "SKILL.md", "# Nested\n## Description\nd\n## Instructions\ni\n")
    loader = SkillFileLoader()
    skills = loader.load_dir(tmp_path, recursive=False)
    assert [s.name for s in skills] == ["Top"]


def test_load_dir_missing_root_raises(tmp_path: Path) -> None:
    loader = SkillFileLoader()
    with pytest.raises(SkillFileLoadError):
        loader.load_dir(tmp_path / "does-not-exist")


def test_load_dir_empty_returns_empty_list(tmp_path: Path) -> None:
    loader = SkillFileLoader()
    assert loader.load_dir(tmp_path) == []


def test_load_dir_single_failure_fails_fast(tmp_path: Path) -> None:
    _write(tmp_path / "good" / "SKILL.md", "# G\n## Description\nd\n## Instructions\ni\n")
    _write(tmp_path / "bad" / "SKILL.md", "---\nname: [x\n---\nbody\n")
    loader = SkillFileLoader()
    with pytest.raises(SkillFileFormatError):
        loader.load_dir(tmp_path)


def test_load_many_preserves_input_order(tmp_path: Path) -> None:
    p1 = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    p2 = _write(tmp_path / "b" / "SKILL.md", "# B\n## Description\ndb\n## Instructions\nib\n")
    loader = SkillFileLoader()
    skills = loader.load_many([str(p2), str(p1)])
    assert [s.name for s in skills] == ["B", "A"]


def test_default_max_file_size_constant() -> None:
    assert DEFAULT_MAX_FILE_SIZE_BYTES == 256_000


def test_skill_metadata_source_is_file(tmp_path: Path) -> None:
    p = _write(tmp_path / "x" / "SKILL.md", "# X\n## Description\nd\n## Instructions\ni\n")
    loader = SkillFileLoader()
    skill = loader.load_file(p)
    assert skill.metadata["source"] == "file"
    assert "path" in skill.metadata
    assert skill.metadata["loader"] == "SkillFileLoader"


def test_skill_loader_load_file_returns_skill_subclass(tmp_path: Path) -> None:
    p = _write(tmp_path / "x" / "SKILL.md", "# X\n## Description\nd\n## Instructions\ni\n")
    loader = SkillFileLoader()
    skill = loader.load_file(p)
    assert isinstance(skill, Skill)
