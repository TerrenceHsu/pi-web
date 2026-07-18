"""CredentialRuntimeConfig + WebSecurityConfig tests（P1-E1-4A）.

覆盖 spec §10 Config 测试 1–10：
- auto/keyring/memory 正常解析
- 未知 backend 拒绝
- 相对 DB path 拒绝
- :memory: 拒绝
- SQLite URI 拒绝
- ~ 正确展开
- 路径在 App 创建后不受 cwd 改变
- 默认 Host 不含 wildcard
- body limit 默认 32 KiB
- 测试配置可显式加入 testserver
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.credentials_runtime import (
    CredentialRuntimeConfigError,
    build_credential_runtime_config,
)
from pi_agent_core_py.web.local_web_security import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    WebSecurityConfig,
    default_web_security_config,
)

# ============================================================================
# WebSecurityConfig
# ============================================================================


class TestWebSecurityConfig:
    def test_default_allowed_hosts_no_wildcard(self) -> None:
        """生产默认 Host 不含 '*' / 'testserver'."""
        assert "*" not in DEFAULT_ALLOWED_HOSTS
        assert "testserver" not in DEFAULT_ALLOWED_HOSTS
        assert "localhost" in DEFAULT_ALLOWED_HOSTS
        assert "127.0.0.1" in DEFAULT_ALLOWED_HOSTS
        assert "::1" in DEFAULT_ALLOWED_HOSTS

    def test_default_body_limit_is_32_kib(self) -> None:
        assert DEFAULT_MAX_REQUEST_BODY_BYTES == 32 * 1024

    def test_default_config_uses_defaults(self) -> None:
        c = default_web_security_config()
        assert c.allowed_hosts == DEFAULT_ALLOWED_HOSTS
        assert c.max_request_body_bytes == DEFAULT_MAX_REQUEST_BODY_BYTES
        assert c.require_ui_header is True
        assert c.allowed_ui_origins == ()

    def test_extra_hosts_added_explicitly(self) -> None:
        c = default_web_security_config(extra_hosts=("testserver",))
        assert "testserver" in c.allowed_hosts
        # 默认 hosts 仍保留
        assert "localhost" in c.allowed_hosts

    def test_wildcard_in_extra_hosts_rejected(self) -> None:
        with pytest.raises(ValueError, match="wildcard"):
            default_web_security_config(extra_hosts=("*",))

    def test_extra_ui_origins_added(self) -> None:
        c = default_web_security_config(
            extra_ui_origins=("http://localhost:5173",)
        )
        assert "http://localhost:5173" in c.allowed_ui_origins

    def test_config_is_frozen(self) -> None:
        c = default_web_security_config()
        # FrozenInstanceError from dataclasses
        with pytest.raises(AttributeError):
            c.allowed_hosts = ("evil.com",)  # type: ignore[misc]


# ============================================================================
# build_credential_runtime_config——backend mode
# ============================================================================


class TestBackendMode:
    def _ws(self) -> WebSecurityConfig:
        return default_web_security_config()

    def test_auto_mode_accepted(self, tmp_path: Path) -> None:
        cfg = build_credential_runtime_config(
            database_path=str(tmp_path / "creds.db"),
            secret_backend_mode="auto",
            web_security=self._ws(),
        )
        assert cfg.secret_backend_mode == "auto"

    def test_keyring_mode_accepted(self, tmp_path: Path) -> None:
        cfg = build_credential_runtime_config(
            database_path=str(tmp_path / "creds.db"),
            secret_backend_mode="keyring",
            web_security=self._ws(),
        )
        assert cfg.secret_backend_mode == "keyring"

    def test_memory_mode_accepted(self, tmp_path: Path) -> None:
        cfg = build_credential_runtime_config(
            database_path=str(tmp_path / "creds.db"),
            secret_backend_mode="memory",
            web_security=self._ws(),
        )
        assert cfg.secret_backend_mode == "memory"

    def test_unknown_backend_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialRuntimeConfigError, match="unknown"):
            build_credential_runtime_config(
                database_path=str(tmp_path / "creds.db"),
                secret_backend_mode="evil",
                web_security=self._ws(),
            )

    def test_unknown_backend_does_not_open_resources(
        self, tmp_path: Path
    ) -> None:
        """Config 阶段失败——不应该产生任何 owned resource."""
        # 静态校验——build_credential_runtime_config 只做 validation，不开 connection
        with pytest.raises(CredentialRuntimeConfigError):
            build_credential_runtime_config(
                database_path=str(tmp_path / "creds.db"),
                secret_backend_mode="redis",  # type: ignore[arg-type]
                web_security=self._ws(),
            )


# ============================================================================
# DB path resolution
# ============================================================================


class TestDbPathResolution:
    def _ws(self) -> WebSecurityConfig:
        return default_web_security_config()

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        rel = "creds.db"
        with pytest.raises(CredentialRuntimeConfigError, match="absolute"):
            build_credential_runtime_config(
                database_path=rel,
                secret_backend_mode="auto",
                web_security=self._ws(),
            )

    def test_memory_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialRuntimeConfigError, match=":memory:"):
            build_credential_runtime_config(
                database_path=":memory:",
                secret_backend_mode="auto",
                web_security=self._ws(),
            )

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialRuntimeConfigError, match="non-empty"):
            build_credential_runtime_config(
                database_path="",
                secret_backend_mode="auto",
                web_security=self._ws(),
            )

    def test_sqlite_uri_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialRuntimeConfigError, match="URI"):
            build_credential_runtime_config(
                database_path="file:creds.db?mode=ro",
                secret_backend_mode="auto",
                web_security=self._ws(),
            )

    def test_sqlite_uri_form_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialRuntimeConfigError, match="URI"):
            build_credential_runtime_config(
                database_path="file:memory:?cache=shared",
                secret_backend_mode="auto",
                web_security=self._ws(),
            )

    def test_tilde_expanded(self, tmp_path: Path, monkeypatch) -> None:
        """~ 应展开为用户 home."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
        cfg = build_credential_runtime_config(
            database_path="~/creds.db",
            secret_backend_mode="auto",
            web_security=self._ws(),
        )
        assert cfg.database_path == (tmp_path / "creds.db").resolve()

    def test_absolute_path_preserved(self, tmp_path: Path) -> None:
        abs_path = str(tmp_path / "creds.db")
        cfg = build_credential_runtime_config(
            database_path=abs_path,
            secret_backend_mode="auto",
            web_security=self._ws(),
        )
        assert cfg.database_path == Path(abs_path).resolve()

    def test_path_object_accepted(self, tmp_path: Path) -> None:
        cfg = build_credential_runtime_config(
            database_path=tmp_path / "creds.db",
            secret_backend_mode="auto",
            web_security=self._ws(),
        )
        assert isinstance(cfg.database_path, Path)

    def test_path_immutable_after_resolve(self, tmp_path: Path, monkeypatch) -> None:
        """Config 创建后 cwd 改变不影响 database_path."""
        cfg = build_credential_runtime_config(
            database_path=str(tmp_path / "creds.db"),
            secret_backend_mode="auto",
            web_security=self._ws(),
        )
        original = cfg.database_path

        # Change cwd——config should keep original absolute path
        other_dir = tmp_path / "other_cwd"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        assert cfg.database_path == original


# ============================================================================
# CredentialRuntimeConfig frozen
# ============================================================================


class TestRuntimeConfigFrozen:
    def test_config_is_frozen(self, tmp_path: Path) -> None:
        cfg = build_credential_runtime_config(
            database_path=str(tmp_path / "creds.db"),
            secret_backend_mode="auto",
            web_security=default_web_security_config(),
        )
        with pytest.raises(AttributeError):
            cfg.secret_backend_mode = "memory"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            cfg.database_path = Path("/etc/passwd")  # type: ignore[misc]
