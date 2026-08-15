"""CredentialReadiness tests（P1-E1-4A）.

覆盖 spec §10 Readiness 30–35：
- auto + Keyring available → ready
- auto + unavailable → degraded
- keyring + unavailable → degraded
- memory → ready
- Keyring degraded 不影响全局 App 启动
- 未知 backend 安全失败
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.credentials.runtime import (
    CredentialReadiness,
    CredentialRuntimeConfigError,
    build_credential_runtime_config,
    credential_runtime_context,
)
from pi_agent_core_py.web.local_web_security import default_web_security_config


def _cfg(tmp_path: Path, mode: str) -> object:
    return build_credential_runtime_config(
        database_path=str(tmp_path / "creds.db"),
        secret_backend_mode=mode,
        web_security=default_web_security_config(),
    )


class TestReadinessReadyStates:
    async def test_auto_with_keyring_available_is_ready(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pi_agent_core_py.secrets import InMemorySecretStore
        from pi_agent_core_py.web.credentials import runtime as rt

        async def _probe_ok() -> bool:
            return True

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_ok)
        monkeypatch.setattr(
            rt, "OSKeyringSecretStore", lambda: InMemorySecretStore(), raising=False
        )
        async with credential_runtime_context(_cfg(tmp_path, "auto")) as runtime:  # type: ignore[arg-type]
            r = runtime.readiness
            assert r.status == "ready"
            assert r.keyring_available is True
            assert r.reason_code is None
            assert r.configured_backend == "auto"

    async def test_memory_mode_is_ready(self, tmp_path: Path) -> None:
        """memory = user-explicit disable——ready, not degraded."""
        async with credential_runtime_context(_cfg(tmp_path, "memory")) as runtime:  # type: ignore[arg-type]
            r = runtime.readiness
            assert r.status == "ready"
            assert r.configured_backend == "memory"
            assert r.keyring_available is False
            assert r.reason_code is None


class TestReadinessDegradedStates:
    async def test_auto_keyring_unavailable_is_degraded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pi_agent_core_py.web.credentials import runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        async with credential_runtime_context(_cfg(tmp_path, "auto")) as runtime:  # type: ignore[arg-type]
            r = runtime.readiness
            assert r.status == "degraded"
            assert r.keyring_available is False
            assert r.reason_code == "keyring_unavailable"

    async def test_keyring_mode_unavailable_is_degraded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pi_agent_core_py.web.credentials import runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        async with credential_runtime_context(_cfg(tmp_path, "keyring")) as runtime:  # type: ignore[arg-type]
            r = runtime.readiness
            assert r.status == "degraded"
            assert r.reason_code == "keyring_unavailable"


class TestReadinessDoesNotBlockApp:
    async def test_degraded_does_not_raise(self, tmp_path: Path, monkeypatch) -> None:
        """Keyring 不可用 → readiness=degraded，但应用仍启动."""
        from pi_agent_core_py.web.credentials import runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        # This should NOT raise——credential_runtime_context yields runtime
        async with credential_runtime_context(_cfg(tmp_path, "auto")) as runtime:  # type: ignore[arg-type]
            assert runtime.readiness.status == "degraded"
            # Service 仍可用——session_only / env 都不受影响
            from pi_agent_core_py.web.credentials.service import (
                CreateCredentialCommand,
            )
            result = await runtime.service.create(
                CreateCredentialCommand(
                    label="OK",
                    storage_mode="session_only",
                    secret_value="sk-test-aaaaaaaaaa",
                )
            )
            assert result.record.id.startswith("cred-")


class TestUnknownBackend:
    def test_unknown_backend_fails_safely(self, tmp_path: Path) -> None:
        """未知 backend → CredentialRuntimeConfigError——不开任何资源."""
        with pytest.raises(CredentialRuntimeConfigError, match="unknown"):
            build_credential_runtime_config(
                database_path=str(tmp_path / "creds.db"),
                secret_backend_mode="redis",
                web_security=default_web_security_config(),
            )

    def test_unknown_backend_safe_message_no_path_leak(self, tmp_path: Path) -> None:
        """Error message 不应泄漏 db_path（虽然不含 secret，但是良好实践）."""
        try:
            build_credential_runtime_config(
                database_path=str(tmp_path / "creds.db"),
                secret_backend_mode="redis",
                web_security=default_web_security_config(),
            )
        except CredentialRuntimeConfigError as e:
            msg = str(e)
            assert str(tmp_path) not in msg
            assert "redis" in msg


class TestReadinessReprSafety:
    def test_readiness_repr_does_not_leak_secrets(self) -> None:
        r = CredentialReadiness(
            status="degraded",
            configured_backend="auto",
            keyring_available=False,
            reason_code="keyring_unavailable",
        )
        text = repr(r)
        assert "PI_E1_SECRET_MARKER" not in text
        assert "api_key" not in text.lower()
