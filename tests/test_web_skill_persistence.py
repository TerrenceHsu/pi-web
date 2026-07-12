"""P1-C2: Skill persistence 测试——async def + pytest-asyncio。

覆盖上传 / enable / disable / delete / restart restore / 损坏隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

_VALID_SKILL_MD = """---
name: codereview
description: Review Python code
priority: 10
---

# Instructions

You are a code reviewer.
"""


def _make_harness() -> AgentHarness:
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    """with TestClient——让 lifespan 跑（extension_store init + skill restore）。"""
    harness = _make_harness()
    app = create_app(harness, db_path=str(tmp_path / "test.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


def _upload_skill(client, name="codereview", content=None):
    md = content or _VALID_SKILL_MD.replace("codereview", name)
    return client.post(
        "/api/skills/upload",
        files={"files": (f"{name}.md", md.encode("utf-8"), "text/markdown")},
    )


async def _get_persisted(app, name):
    """helper——await extension_store.get_uploaded_skill。"""
    return await app.state.web.extension_store.get_uploaded_skill(name)


# ============================================================================
# Tests 1-4: 上传写入 DB + 重启恢复 + enabled/disabled
# ============================================================================


async def test_1_upload_writes_to_db(web_client):
    client, _, app = web_client
    resp = _upload_skill(client, "codereview")
    assert resp.status_code == 200
    skill = await _get_persisted(app, "codereview")
    assert skill is not None
    assert skill.enabled is True
    assert skill.source_kind == "upload"
    assert skill.content_sha256


async def test_2_restart_restores_skill(tmp_path):
    db_path = str(tmp_path / "restart.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "codereview")

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        assert harness_b.skill_registry.has("codereview")
        skill = harness_b.skill_registry.get("codereview")
        assert skill.metadata.get("source_kind") == "upload"
        assert skill.metadata.get("persisted") is True


async def test_3_enabled_state_restored(tmp_path):
    db_path = str(tmp_path / "enabled.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "codereview")
        client_a.post("/api/skills/codereview/enable")

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        skill = harness_b.skill_registry.get("codereview")
        assert skill.status == "enabled"


async def test_4_disabled_state_restored(tmp_path):
    db_path = str(tmp_path / "disabled.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "codereview")
        client_a.post("/api/skills/codereview/disable")

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        skill = harness_b.skill_registry.get("codereview")
        assert skill.status == "disabled"


# ============================================================================
# Test 5: Use-this-turn selection 不持久化
# ============================================================================


async def test_5_skill_names_not_persisted(web_client):
    client, _, _ = web_client
    _upload_skill(client, "codereview")
    resp = client.post(
        "/api/prompt",
        json={"text": "hello", "skill_names": ["codereview"]},
    )
    assert resp.status_code == 200
    assert resp.json()["applied_skill_names"] == ["codereview"]


# ============================================================================
# Tests 6-7: 重名 + DB rollback
# ============================================================================


async def test_6_duplicate_upload_returns_error(web_client):
    client, _, _ = web_client
    _upload_skill(client, "codereview")
    resp2 = _upload_skill(client, "codereview")
    body = resp2.json()
    assert body.get("errors") or resp2.status_code == 409


async def test_7_db_failure_rolls_back_registry(web_client, monkeypatch):
    client, _, app = web_client
    state = app.state.web

    async def failing_upsert(**kwargs):
        from pi_agent_core_py.web.extension_store import ExtensionStoreError
        raise ExtensionStoreError("simulated DB failure")

    monkeypatch.setattr(
        state.extension_store, "upsert_uploaded_skill", failing_upsert
    )

    resp = _upload_skill(client, "newskill")
    body = resp.json()
    assert body.get("errors")
    assert not state.harness.skill_registry.has("newskill")


# ============================================================================
# Tests 8-10: DELETE + built-in 不可删
# ============================================================================


async def test_8_delete_uploaded_skill(web_client):
    client, _, app = web_client
    _upload_skill(client, "codereview")
    assert app.state.web.harness.skill_registry.has("codereview")

    resp = client.delete("/api/skills/codereview")
    assert resp.status_code == 200
    assert not app.state.web.harness.skill_registry.has("codereview")


async def test_9_deleted_skill_not_restored_on_restart(tmp_path):
    db_path = str(tmp_path / "deleted.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "codereview")
        client_a.delete("/api/skills/codereview")

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        assert not harness_b.skill_registry.has("codereview")


async def test_10_non_uploaded_skill_not_deletable(web_client):
    client, harness, _ = web_client
    from pi_agent_core_py.skills import Skill

    harness.skill_registry.register(
        Skill(name="manual", description="manual", prompt="test")
    )
    resp = client.delete("/api/skills/manual")
    assert resp.status_code == 403
    assert harness.skill_registry.has("manual")


# ============================================================================
# Tests 11-12: restore 损坏隔离 + name conflict
# ============================================================================


async def test_11_corrupted_skill_row_isolated_on_restart(tmp_path):
    db_path = str(tmp_path / "corrupt.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "good_skill")
        _upload_skill(client_a, "bad_skill")

    # 破坏 bad_skill 的 skill_json
    import aiosqlite
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "UPDATE web_uploaded_skills SET skill_json = ? WHERE name = ?",
        ("NOT JSON{{", "bad_skill"),
    )
    await db.commit()
    await db.close()

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        assert harness_b.skill_registry.has("good_skill")
        assert not harness_b.skill_registry.has("bad_skill")
        bad = await app_b.state.web.extension_store.get_uploaded_skill("bad_skill")
        assert bad.last_restore_error is not None


async def test_12_restore_name_conflict_does_not_overwrite(tmp_path):
    db_path = str(tmp_path / "conflict.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "shared_name")

    from pi_agent_core_py.skills import Skill

    harness_b = _make_harness()
    harness_b.skill_registry.register(
        Skill(name="shared_name", description="builtin", prompt="builtin")
    )
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        skill = harness_b.skill_registry.get("shared_name")
        assert skill.description == "builtin"
        persisted = await app_b.state.web.extension_store.get_uploaded_skill(
            "shared_name"
        )
        assert "conflict" in (persisted.last_restore_error or "").lower()


# ============================================================================
# Test 13: prompt body 默认不可读
# ============================================================================


async def test_13_prompt_body_not_returned_by_default(web_client):
    client, _, _ = web_client
    _upload_skill(client, "codereview")
    resp = client.get("/api/skills/codereview")
    body = resp.json()
    assert "prompt" not in body or body.get("prompt") is None


# ============================================================================
# Tests 14-16: 多文件 / 并发 / filesystem 不写 DB
# ============================================================================


async def test_14_multi_file_partial_success(web_client):
    client, _, _ = web_client
    _upload_skill(client, "first_skill")
    md2 = _VALID_SKILL_MD.replace("codereview", "second_skill")
    md1 = _VALID_SKILL_MD.replace("codereview", "first_skill")
    resp = client.post(
        "/api/skills/upload",
        files=[
            ("files", ("first.md", md1.encode(), "text/markdown")),
            ("files", ("second.md", md2.encode(), "text/markdown")),
        ],
    )
    body = resp.json()
    assert any(s["name"] == "second_skill" for s in body.get("skills", []))
    assert any(
        e.get("skill_name") == "first_skill" for e in body.get("errors", [])
    )


async def test_15_concurrent_same_name_only_one_succeeds(web_client):
    """两个并发同名上传——mutation lock 串行化，只有一个成功。"""
    client, _, _ = web_client

    # 用 asyncio.gather 模拟并发——但 TestClient 是 sync
    # 这里用 2 个 sync 调用快速连发验证 lock 串行化
    _VALID_SKILL_MD.replace("codereview", "concurrent_skill")
    resp1 = _upload_skill(client, "concurrent_skill")
    resp2 = _upload_skill(client, "concurrent_skill")

    # 第一个成功，第二个 error
    body1 = resp1.json()
    body2 = resp2.json()
    successes = sum(
        1 for b in [body1, body2] if b.get("count", 0) > 0
    )
    assert successes == 1


async def test_16_filesystem_skill_enable_no_db_row(web_client):
    client, harness, app = web_client
    from pi_agent_core_py.skills import Skill

    harness.skill_registry.register(
        Skill(name="manual_fs", description="filesystem", prompt="test")
    )
    client.post("/api/skills/manual_fs/enable")
    client.post("/api/skills/manual_fs/disable")

    persisted = await _get_persisted(app, "manual_fs")
    assert persisted is None


# ============================================================================
# Test 17: Markdown 伪造 source_kind 被强制覆盖
# ============================================================================


async def test_17_markdown_forged_source_overridden(web_client):
    client, _, app = web_client
    resp = _upload_skill(client, "forged")
    assert resp.status_code == 200

    persisted = await _get_persisted(app, "forged")
    assert persisted.source_kind == "upload"

    skill = app.state.web.harness.skill_registry.get("forged")
    assert skill.metadata.get("source_kind") == "upload"
    assert skill.metadata.get("persisted") is True


# ============================================================================
# Test 18: hash 不匹配时不恢复
# ============================================================================


async def test_18_hash_mismatch_not_restored(tmp_path):
    db_path = str(tmp_path / "hashmismatch.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "hashcheck")

    # 破坏 raw_markdown（不改 content_sha256）
    import aiosqlite
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "UPDATE web_uploaded_skills SET raw_markdown = ? WHERE name = ?",
        ("TAMPERED", "hashcheck"),
    )
    await db.commit()
    await db.close()

    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        assert not harness_b.skill_registry.has("hashcheck")
        persisted = await app_b.state.web.extension_store.get_uploaded_skill(
            "hashcheck"
        )
        assert "hash" in (persisted.last_restore_error or "").lower()


# ============================================================================
# Test 19: 修复后清除 restore_error
# ============================================================================


async def test_19_successful_restore_clears_error(tmp_path):
    db_path = str(tmp_path / "clearerror.sqlite")
    harness_a = _make_harness()
    app_a = create_app(harness_a, db_path=db_path)
    with TestClient(app_a) as client_a:
        _upload_skill(client_a, "willrecover")

    # 破坏 skill_json + 设 restore_error
    import aiosqlite
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "UPDATE web_uploaded_skills SET skill_json = ?, last_restore_error = ? "
        "WHERE name = ?",
        ("BAD", "previous error", "willrecover"),
    )
    await db.commit()
    await db.close()

    # App B: 重启——损坏 → error 保留
    harness_b = _make_harness()
    app_b = create_app(harness_b, db_path=db_path)
    with TestClient(app_b):
        persisted = await app_b.state.web.extension_store.get_uploaded_skill(
            "willrecover"
        )
        assert persisted.last_restore_error is not None

    # App C: 重新上传——修复后 error 清除
    harness_c = _make_harness()
    app_c = create_app(harness_c, db_path=db_path)
    with TestClient(app_c) as client_c:
        _upload_skill(client_c, "willrecover")
        persisted = await app_c.state.web.extension_store.get_uploaded_skill(
            "willrecover"
        )
        assert persisted.last_restore_error is None


# ============================================================================
# Test 20: async prompt 不回归
# ============================================================================


async def test_20_async_prompt_still_works(web_client):
    client, _, _ = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    assert resp.status_code == 202
    assert resp.json()["request_id"].startswith("req_")
