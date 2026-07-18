"""App lifespan integration tests for CredentialRuntimeState（P1-E1-4A commit 2）.

覆盖 spec §10 Lifespan / Path 中需要 app-level 集成的场景：
- 启动后 app.state.credential_runtime 被填入
- shutdown 后 app.state.credential_runtime = None
- enable_credential_runtime=False 跳过 credential runtime
- db_path=None（:memory:）跳过 credential runtime（向后兼容）
- TrustedHost middleware 默认未安装
- enable_trusted_host=True + extra_hosts testserver——TestClient 仍可访问
- enable_trusted_host=True 拒绝外部 Host
- 现有 API 在不安装 TrustedHost 时行为不变
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.web.app import create_app

# ============================================================================
# Fixtures
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [
            [TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())],
        ]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


# ============================================================================
# Credential runtime lifespan integration
# ============================================================================


class TestCredentialRuntimeLifespan:
    async def test_lifespan_populates_credential_runtime(self, tmp_path: Path) -> None:
        """File db_path + enable → app.state.credential_runtime is populated."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=True,
            credential_secret_backend="memory",
        )
        with TestClient(app, base_url="http://testserver") as client:
            # Inside lifespan——credential_runtime should be populated
            assert app.state.credential_runtime is not None
            # Readiness——memory mode → ready
            assert app.state.credential_runtime.readiness.status == "ready"
            # Sanity ping
            r = client.get("/api/sessions")
            # 200 or other API response——not 500
            assert r.status_code < 500
        # After lifespan exit——cleared
        assert app.state.credential_runtime is None

    async def test_lifespan_skips_credential_runtime_when_disabled(
        self, tmp_path: Path
    ) -> None:
        """enable_credential_runtime=False → app.state.credential_runtime stays None."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=False,
        )
        with TestClient(app):
            assert app.state.credential_runtime is None

    async def test_lifespan_skips_credential_runtime_for_memory_db(
        self
    ) -> None:
        """db_path=None（:memory:）→ 跳过 credential runtime（向后兼容）."""
        h = _harness()
        app = create_app(h, db_path=None)
        with TestClient(app):
            assert app.state.credential_runtime is None

    async def test_auto_enable_when_db_path_is_file(self, tmp_path: Path) -> None:
        """enable_credential_runtime=None（默认）+ file db_path → 自动启用."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            credential_secret_backend="memory",
        )
        with TestClient(app):
            assert app.state.credential_runtime is not None

    async def test_auto_enable_skipped_for_memory_db(self) -> None:
        """enable_credential_runtime=None + db_path=None → 跳过（向后兼容）."""
        h = _harness()
        app = create_app(h, db_path=None)
        with TestClient(app):
            assert app.state.credential_runtime is None


# ============================================================================
# Credential runtime config error → startup fails
# ============================================================================


class TestCredentialRuntimeConfigError:
    async def test_unknown_backend_fails_startup(self, tmp_path: Path) -> None:
        """未知 backend → startup RuntimeError（不静默降级）."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=True,
            credential_secret_backend="redis",  # type: ignore[arg-type]
        )
        with pytest.raises((RuntimeError, ValueError)):
            with TestClient(app):
                pass


# ============================================================================
# TrustedHost middleware integration
# ============================================================================


class TestTrustedHostMiddleware:
    async def test_trusted_host_default_off(self, tmp_path: Path) -> None:
        """默认 enable_trusted_host=False——TestClient（Host: testserver）正常."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=False,
        )
        with TestClient(app) as client:  # default base_url=testserver
            r = client.get("/api/sessions")
            # 没有 TrustedHost——Host: testserver 允许
            assert r.status_code != 400

    async def test_trusted_host_with_testserver_explicit(
        self, tmp_path: Path
    ) -> None:
        """enable_trusted_host=True + extra testserver——TestClient 可访问."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=False,
            enable_trusted_host=True,
            credential_extra_hosts=("testserver",),
        )
        with TestClient(app) as client:
            r = client.get("/api/sessions")
            assert r.status_code != 400

    async def test_trusted_host_rejects_external_host(
        self, tmp_path: Path
    ) -> None:
        """enable_trusted_host=True → 外部 Host 被 400 拒绝."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=False,
            enable_trusted_host=True,
            credential_extra_hosts=("testserver",),
        )
        with TestClient(app) as client:
            r = client.get("/api/sessions", headers={"Host": "evil.com"})
            assert r.status_code == 400

    async def test_trusted_host_rejects_testserver_when_not_explicit(
        self, tmp_path: Path
    ) -> None:
        """enable_trusted_host=True + 不加 testserver → TestClient 默认 Host 被拒."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=False,
            enable_trusted_host=True,
            # No extra_hosts——testserver NOT allowed
        )
        with TestClient(app) as client:
            r = client.get("/api/sessions")
            assert r.status_code == 400


# ============================================================================
# Restart recovery via app lifespan
# ============================================================================


class TestAppRestartRecovery:
    async def test_credential_record_survives_app_restart(
        self, tmp_path: Path
    ) -> None:
        """Create credential in app1 → close → reopen app2 → record persists."""
        h1 = _harness()
        app1 = create_app(
            h1,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=True,
            credential_secret_backend="memory",
        )
        with TestClient(app1):
            # Create a credential via service（直接调 service，不用 REST——E1-4B）
            from pi_agent_core_py.web.credentials_service import (
                CreateCredentialCommand,
            )

            runtime = app1.state.credential_runtime
            assert runtime is not None
            result = await runtime.service.create(
                CreateCredentialCommand(
                    label="Persist",
                    storage_mode="session_only",
                    secret_value="sk-test-aaaaaaaaaa",
                )
            )
            cred_id = result.record.id

        # Reopen——new app instance, same db_path
        h2 = _harness()
        app2 = create_app(
            h2,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=True,
            credential_secret_backend="memory",
        )
        with TestClient(app2):
            runtime = app2.state.credential_runtime
            assert runtime is not None
            row = await runtime.repository.get(cred_id)
            assert row.label == "Persist"

    async def test_restart_does_not_leave_half_initialized_runtime(
        self, tmp_path: Path
    ) -> None:
        """Normal restart leaves no half-initialized state."""
        h = _harness()
        app = create_app(
            h,
            db_path=str(tmp_path / "app.db"),
            enable_credential_runtime=True,
            credential_secret_backend="memory",
        )
        with TestClient(app):
            assert app.state.credential_runtime is not None
        assert app.state.credential_runtime is None
