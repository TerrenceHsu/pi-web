"""P0-4 Step 1: Web Skills management API.

覆盖：
- POST /api/skills/upload：合法 SKILL.md 上传成功
- 重名上传 → 409
- 格式错误（frontmatter YAML 不合法 / 非 utf-8）→ 400
- 大小超限 → 400 (SkillFileSecurityError)
- POST /api/skills/{name}/enable / disable：切换 status；不存在 → 404
- GET /api/skills/{name}：默认不返回 prompt
- GET /api/skills/{name}?include_prompt=true 默认 403
- allow_prompt_preview=True + localhost → 200 含 prompt
- POST /api/prompt 顶层 skill_names：进入 metadata + response.applied_skill_names
- POST /api/prompt skill_names 与 skill_selection.names 合并去重
- POST /api/prompt 重名 skill（不存在）→ 由 registry 抛错走 500 路径

不动 core runtime（loop / agent / context / providers）。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app

# ============================================================================
# fixtures
# ============================================================================


def _make_harness() -> AgentHarness:
    """空 skill_registry 的 harness——upload 时由前端路径驱动注册。"""
    scripts = [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    # 显式 attach 一个空 SkillRegistry——保证 upload endpoint 走 register 路径，
    # 而不是 422
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client():
    """默认 allow_prompt_preview=False。"""
    harness = _make_harness()
    app = create_app(harness)
    client = TestClient(app)
    try:
        yield client, harness, app
    finally:
        client.close()
        dispose_app(app)


@pytest.fixture
def web_client_with_preview():
    """开 allow_prompt_preview=True——单测 prompt preview 通过路径。"""
    harness = _make_harness()
    app = create_app(harness, allow_prompt_preview=True)
    client = TestClient(app)
    try:
        yield client, harness, app
    finally:
        client.close()
        dispose_app(app)


_VALID_SKILL_MD = """---
name: coding_review
description: Review Python agent runtime code
priority: 20
tags: ["coding", "review"]
tool_names: ["read_file", "run_tests"]
---

你是一个严格的 Python agent runtime 代码审查助手。

重点检查：
1. async 生命周期
2. event 顺序
3. tool error isolation
"""

_VALID_SKILL_MD_2 = """---
name: finance_helper
description: Analyze finance data
priority: 30
tags: ["finance"]
---

你是一个金融分析助手。
"""

_BAD_FRONTMATTER_MD = """---
name: bad
description: [unclosed
---

body
"""

_NON_UTF8 = b"\xff\xfe\x00bad"


def _skill_upload_tuple(filename: str, content: str | bytes):
    """构造 multipart upload tuple——避免每行写长 io.BytesIO(...) 行。"""
    if isinstance(content, str):
        content = content.encode()
    return (filename, io.BytesIO(content), "text/markdown")


# ============================================================================
# 1. POST /api/skills/upload
# ============================================================================


def test_upload_skill_success(web_client):
    """合法 SKILL.md 上传成功；response 不含 prompt 正文。"""
    client, harness, _ = web_client
    r = client.post(
        "/api/skills/upload",
        files=[("files", _skill_upload_tuple("coding_review.md", _VALID_SKILL_MD))],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    skill = data["skills"][0]
    assert skill["name"] == "coding_review"
    assert skill["description"] == "Review Python agent runtime code"
    assert skill["priority"] == 20
    assert skill["tags"] == ["coding", "review"]
    assert skill["tool_names"] == ["read_file", "run_tests"]
    assert "prompt" not in skill  # 默认不返回 prompt
    # registry 中确实有这条 skill
    assert harness.skill_registry.has("coding_review")


def test_upload_skill_duplicate_returns_409(web_client):
    """重名上传 → 409，detail.skill_name 正确。"""
    client, _, _ = web_client
    # 第一次上传
    r1 = client.post(
        "/api/skills/upload",
        files=[("files", _skill_upload_tuple("coding_review.md", _VALID_SKILL_MD))],
    )
    assert r1.status_code == 200
    # 第二次重名
    r2 = client.post(
        "/api/skills/upload",
        files=[("files", _skill_upload_tuple("coding_review.md", _VALID_SKILL_MD))],
    )
    assert r2.status_code == 409
    err = r2.json()["errors"][0]
    assert err["error_type"] == "SkillRegistrationError"
    assert err["skill_name"] == "coding_review"


def test_upload_skill_format_error_returns_400(web_client):
    """frontmatter YAML 不合法 → 400。"""
    client, _, _ = web_client
    r = client.post(
        "/api/skills/upload",
        files=[("files", ("bad.md", io.BytesIO(_BAD_FRONTMATTER_MD.encode()), "text/markdown"))],
    )
    assert r.status_code == 400
    err = r.json()["errors"][0]
    assert err["error_type"] == "SkillFileFormatError"


def test_upload_skill_non_utf8_returns_400(web_client):
    """非 utf-8 文件 → 400 SkillFileFormatError。"""
    client, _, _ = web_client
    r = client.post(
        "/api/skills/upload",
        files=[("files", ("binary.md", io.BytesIO(_NON_UTF8), "text/markdown"))],
    )
    assert r.status_code == 400
    err = r.json()["errors"][0]
    assert err["error_type"] == "SkillFileFormatError"


def test_upload_skill_too_large_returns_400(web_client):
    """超过 256KB → 400 SkillFileSecurityError。"""
    client, _, _ = web_client
    # 制造 256KB+1B 的内容
    big = ("# big\n\n" + ("x" * 260_000)).encode()
    r = client.post(
        "/api/skills/upload",
        files=[("files", ("big.md", io.BytesIO(big), "text/markdown"))],
    )
    assert r.status_code == 400
    err = r.json()["errors"][0]
    assert err["error_type"] == "SkillFileSecurityError"


def test_upload_skill_no_files_returns_400(web_client):
    """没传任何 file → 400。"""
    client, _, _ = web_client
    r = client.post("/api/skills/upload", files=[])
    assert r.status_code == 400


def test_upload_skill_partial_success_returns_207(web_client):
    """多文件上传部分成功 → 207 Multi-Status。"""
    client, _, _ = web_client
    r = client.post(
        "/api/skills/upload",
        files=[
            ("files", ("good.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown")),
            ("files", ("bad.md", io.BytesIO(_BAD_FRONTMATTER_MD.encode()), "text/markdown")),
        ],
    )
    assert r.status_code == 207
    data = r.json()
    assert data["count"] == 1
    assert data["skills"][0]["name"] == "coding_review"
    assert len(data["errors"]) == 1
    assert data["errors"][0]["error_type"] == "SkillFileFormatError"


def test_upload_skill_no_registry_returns_422():
    """harness 没 attach skill_registry → 422。"""
    scripts = [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)  # 不 attach_skills
    app = create_app(harness)
    try:
        client = TestClient(app)
        r = client.post(
            "/api/skills/upload",
            files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
        )
        assert r.status_code == 422
    finally:
        client.close()
        dispose_app(app)


# ============================================================================
# 2. enable / disable
# ============================================================================


def test_enable_disable_skill_roundtrip(web_client):
    """enable / disable 切换 status；GET /api/skills 反映出来。"""
    client, _, _ = web_client
    # 先上传
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    # disable
    r1 = client.post("/api/skills/coding_review/disable")
    assert r1.status_code == 200
    assert r1.json()["status"] == "disabled"
    # GET /api/skills 应反映
    skills = client.get("/api/skills").json()["skills"]
    assert next(s for s in skills if s["name"] == "coding_review")["status"] == "disabled"
    # enable
    r2 = client.post("/api/skills/coding_review/enable")
    assert r2.status_code == 200
    assert r2.json()["status"] == "enabled"


def test_enable_skill_not_found_returns_404(web_client):
    """enable 不存在的 skill → 404。"""
    client, _, _ = web_client
    r = client.post("/api/skills/no_such_skill/enable")
    assert r.status_code == 404
    assert "no_such_skill" in r.json()["detail"]


def test_disable_skill_not_found_returns_404(web_client):
    """disable 不存在的 skill → 404。"""
    client, _, _ = web_client
    r = client.post("/api/skills/no_such_skill/disable")
    assert r.status_code == 404


# ============================================================================
# 3. GET /api/skills/{name}
# ============================================================================


def test_get_skill_detail_default_no_prompt(web_client):
    """单条详情默认不返回 prompt。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.get("/api/skills/coding_review")
    assert r.status_code == 200
    skill = r.json()
    assert skill["name"] == "coding_review"
    assert "prompt" not in skill


def test_get_skill_not_found_returns_404(web_client):
    """单条详情不存在 → 404。"""
    client, _, _ = web_client
    r = client.get("/api/skills/no_such_skill")
    assert r.status_code == 404


def test_get_skill_include_prompt_default_403(web_client):
    """include_prompt=true 默认 403（allow_prompt_preview=False）。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.get("/api/skills/coding_review?include_prompt=true")
    assert r.status_code == 403


def test_get_skill_include_prompt_with_allow_and_localhost(web_client_with_preview):
    """allow_prompt_preview=True + localhost → 200 含 prompt template。"""
    client, _, _ = web_client_with_preview
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.get("/api/skills/coding_review?include_prompt=true")
    assert r.status_code == 200
    skill = r.json()
    # SkillFileLoader 走 PromptTemplate 路径——返回 {name, template, variables}
    assert "prompt" in skill
    assert isinstance(skill["prompt"], dict)
    assert "template" in skill["prompt"]
    assert "代码审查" in skill["prompt"]["template"] or "Python" in skill["prompt"]["template"]


# ============================================================================
# 4. POST /api/prompt 顶层 skill_names
# ============================================================================


def test_prompt_skill_names_appear_in_response(web_client):
    """POST /api/prompt body.skill_names → response.applied_skill_names。"""
    client, _, _ = web_client
    # 上传一个 skill
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    # 发 prompt
    r = client.post(
        "/api/prompt",
        json={
            "text": "hello",
            "skill_names": ["coding_review"],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["applied_skill_names"] == ["coding_review"]


def test_prompt_skill_names_merges_with_skill_selection(web_client):
    """skill_names（顶层）+ skill_selection.names 合并去重。"""
    client, _, _ = web_client
    # 上传两个 skills
    client.post(
        "/api/skills/upload",
        files=[
            ("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown")),
            ("files", ("b.md", io.BytesIO(_VALID_SKILL_MD_2.encode()), "text/markdown")),
        ],
    )
    r = client.post(
        "/api/prompt",
        json={
            "text": "hi",
            "skill_selection": {"names": ["coding_review"]},
            "skill_names": ["finance_helper"],
        },
    )
    assert r.status_code == 200, r.text
    applied = r.json()["applied_skill_names"]
    # 两个都应启用（去重后顺序：先 selection，后顶层）
    assert "coding_review" in applied
    assert "finance_helper" in applied


def test_prompt_skill_names_dedup(web_client):
    """顶层 skill_names 与 selection.names 重叠 → 去重。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.post(
        "/api/prompt",
        json={
            "text": "hi",
            "skill_selection": {"names": ["coding_review"]},
            "skill_names": ["coding_review"],
        },
    )
    assert r.status_code == 200
    assert r.json()["applied_skill_names"] == ["coding_review"]


def test_prompt_skill_names_invalid_type_returns_400(web_client):
    """skill_names 非 list → 400。"""
    client, _, _ = web_client
    r = client.post(
        "/api/prompt",
        json={"text": "hi", "skill_names": "coding_review"},
    )
    assert r.status_code == 400


def test_prompt_skill_names_empty_string_returns_400(web_client):
    """skill_names 含空字符串 → 400。"""
    client, _, _ = web_client
    r = client.post(
        "/api/prompt",
        json={"text": "hi", "skill_names": [""]},
    )
    assert r.status_code == 400


def test_prompt_no_skill_names_backward_compat(web_client):
    """不传 skill_names 不回归——旧 skill_selection 仍可用。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.post(
        "/api/prompt",
        json={
            "text": "hi",
            "skill_selection": {"names": ["coding_review"]},
        },
    )
    assert r.status_code == 200
    assert r.json()["applied_skill_names"] == ["coding_review"]


def test_prompt_no_skills_at_all_backward_compat(web_client):
    """完全不传 skill_names / skill_selection → applied_skill_names=[]。"""
    client, _, _ = web_client
    r = client.post("/api/prompt", json={"text": "hello"})
    assert r.status_code == 200
    assert r.json()["applied_skill_names"] == []


# ============================================================================
# 4b: P0-4 Step 2 顺手修——unknown skill_names 不再走 500
# ============================================================================


def test_prompt_unknown_skill_name_returns_400(web_client):
    """skill_names 传不存在的 skill → 400（不再 500）。

    Step 1 报告中的剩余风险：SkillRegistry.select 在 name 不存在时抛
    SkillNotFoundError，会让 /api/prompt 走 500。Step 2 在 web 层加预校验。
    """
    client, _, _ = web_client
    r = client.post(
        "/api/prompt",
        json={"text": "hi", "skill_names": ["no_such_skill_xyz"]},
    )
    assert r.status_code == 400
    data = r.json()
    assert "Unknown skill" in data["detail"]
    assert "no_such_skill_xyz" in data["missing_skill_names"]


def test_prompt_unknown_skill_in_selection_returns_400(web_client):
    """skill_selection.names 含未知 → 400（与顶层 skill_names 一致）。"""
    client, _, _ = web_client
    r = client.post(
        "/api/prompt",
        json={
            "text": "hi",
            "skill_selection": {"names": ["missing_one", "missing_two"]},
        },
    )
    assert r.status_code == 400
    data = r.json()
    # 至少报告第一个缺失的 skill
    assert "missing_one" in data["missing_skill_names"]


def test_prompt_partial_unknown_skill_returns_400(web_client):
    """混合：存在的 + 不存在的 → 仍 400。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", _skill_upload_tuple("a.md", _VALID_SKILL_MD))],
    )
    r = client.post(
        "/api/prompt",
        json={"text": "hi", "skill_names": ["coding_review", "missing_x"]},
    )
    assert r.status_code == 400
    assert "missing_x" in r.json()["missing_skill_names"]


# ============================================================================
# 5. 回归：旧 GET /api/skills 列表不破坏
# ============================================================================


def test_get_skills_list_after_upload(web_client):
    """上传后 GET /api/skills 含上传的 skill。"""
    client, _, _ = web_client
    client.post(
        "/api/skills/upload",
        files=[("files", ("a.md", io.BytesIO(_VALID_SKILL_MD.encode()), "text/markdown"))],
    )
    r = client.get("/api/skills")
    assert r.status_code == 200
    data = r.json()
    assert data["attached"] is True
    names = [s["name"] for s in data["skills"]]
    assert "coding_review" in names
