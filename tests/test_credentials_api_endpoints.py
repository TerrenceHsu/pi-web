"""Credential API endpoint tests（P1-E1-4B2）.

覆盖 8 个 endpoints——使用真实 CredentialService + tmp_path SQLite.

不接真实网络——Validation 测试用注入的 FakeStrategy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.providers.registry import (  # noqa: E402
    ProviderRegistry,
    list_provider_definitions,
)
from pi_agent_core_py.secrets import (  # noqa: E402
    EnvSecretStore,
    InMemorySecretStore,
)
from pi_agent_core_py.web.credentials.api import (  # noqa: E402
    CredentialBodyLimitMiddleware,
    build_full_credential_router,
)
from pi_agent_core_py.web.credentials.runtime import (  # noqa: E402
    CredentialRuntimeState,
    build_credential_runtime_config,
)
from pi_agent_core_py.web.credentials.service import CredentialService  # noqa: E402
from pi_agent_core_py.web.credentials.store import SQLiteCredentialStore  # noqa: E402
from pi_agent_core_py.web.local_web_security import (  # noqa: E402
    default_web_security_config,
)
from pi_agent_core_py.web.provider_validation import (  # noqa: E402
    ProviderValidationResult,
    ValidationStrategyRegistry,
)
from pi_agent_core_py.web.credentials.secret_store import SecretStoreRouter  # noqa: E402

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Fakes
# ============================================================================


class _FakeStrategy:
    """Configurable strategy——records calls, returns set result."""

    def __init__(self, result: ProviderValidationResult) -> None:
        self.provider_id = "anthropic"
        self._result = result
        self.calls: list[str] = []

    async def validate(self, secret: str) -> ProviderValidationResult:
        self.calls.append(secret)
        return self._result


def _ok_result() -> ProviderValidationResult:
    return ProviderValidationResult(
        provider_id="anthropic", valid=True, error_code=None
    )


def _auth_failed() -> ProviderValidationResult:
    return ProviderValidationResult(
        provider_id="anthropic", valid=False, error_code="authentication_failed"
    )


# ============================================================================
# Test app builder
# ============================================================================


def _build_test_app(
    tmp_path: Path,
    *,
    strategy_result: ProviderValidationResult | None = None,
    secret_value_for_strategy: str | None = None,
) -> tuple[FastAPI, _FakeStrategy]:
    """Build app with full Credential API + real Service + fake Strategy."""
    db_path = tmp_path / "creds.db"
    cfg = build_credential_runtime_config(
        database_path=str(db_path),
        secret_backend_mode="memory",
        web_security=default_web_security_config(
            extra_hosts=("testserver",),
            extra_ui_origins=(),
            require_ui_header=False,  # tests don't send UI header
        ),
    )

    # Build router with all endpoints
    router = build_full_credential_router(cfg.web_security)

    # Set up real Service with fake strategy
    fake_strategy = _FakeStrategy(
        strategy_result if strategy_result is not None else _ok_result()
    )

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(
        CredentialBodyLimitMiddleware, max_bytes=cfg.web_security.max_request_body_bytes
    )

    # Lifespan-equivalent——build runtime state for test
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):
        # Open repository
        repo = await SQLiteCredentialStore.open(str(db_path))
        router_store = SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": InMemorySecretStore(),
                "env": EnvSecretStore(),
            }
        )
        service = CredentialService(
            repository=repo,
            router=router_store,
            provider_registry=ProviderRegistry(list_provider_definitions()),
            validation_strategy_registry=ValidationStrategyRegistry(
                strategies={"anthropic_models": fake_strategy}
            ),
        )
        from pi_agent_core_py.web.credentials.runtime import CredentialReadiness

        runtime = CredentialRuntimeState(
            config=cfg,
            repository=repo,
            router=router_store,
            service=service,
            readiness=CredentialReadiness(
                status="ready",
                configured_backend="memory",
                keyring_available=False,
                reason_code=None,
            ),
        )
        _app.state.credential_runtime = runtime
        try:
            yield
        finally:
            try:
                await repo.close()
            except Exception:
                pass
            _app.state.credential_runtime = None

    app.router.lifespan_context = _lifespan
    return app, fake_strategy


def _headers() -> dict:
    """Headers required for Credential API."""
    return {"X-PI-Agent-UI": "1"}


# ============================================================================
# 1. GET /api/provider-definitions
# ============================================================================


class TestProviderDefinitions:
    def test_list_returns_builtin_providers(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.get(
                "/api/provider-definitions", headers=_headers()
            )
            assert r.status_code == 200
            data = r.json()
            ids = [p["id"] for p in data]
            assert "anthropic" in ids
            assert "glm" in ids

    def test_does_not_expose_internal_endpoint(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.get(
                "/api/provider-definitions", headers=_headers()
            )
            text = r.text
            # Internal fields never exposed
            assert "credential_validation_endpoint" not in text
            assert "credential_validation_strategy" not in text
            assert "key_prefix_hints" not in text

    def test_glm_reports_validation_not_supported(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.get(
                "/api/provider-definitions", headers=_headers()
            )
            data = r.json()
            glm = next(p for p in data if p["id"] == "glm")
            assert glm["validation_supported"] is False


# ============================================================================
# 2. POST /api/provider-hints
# ============================================================================


class TestProviderHints:
    def test_anthropic_prefix_detected(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": "sk-ant-test1234567890"},
                headers=_headers(),
            )
            assert r.status_code == 200
            data = r.json()
            assert "anthropic" in data["candidates"]
            assert data["confidence"] == "high"

    def test_hint_does_not_persist(self, tmp_path: Path) -> None:
        """No credential record should exist after hint."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": "sk-ant-test1234567890"},
                headers=_headers(),
            )
            assert r.status_code == 200
            # List credentials——should be empty
            r2 = client.get("/api/credentials", headers=_headers())
            assert r2.json()["credentials"] == []

    def test_hint_response_no_marker(self, tmp_path: Path) -> None:
        """secret_value 含 marker——response 不泄漏."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": SECRET_MARKER},
                headers=_headers(),
            )
            assert r.status_code == 200
            assert SECRET_MARKER not in r.text


# ============================================================================
# 3. GET /api/credentials
# ============================================================================


class TestListCredentials:
    def test_empty_list_initially(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.get("/api/credentials", headers=_headers())
            assert r.status_code == 200
            assert r.json() == {"credentials": []}

    def test_after_create_listed(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            r = client.get("/api/credentials", headers=_headers())
            creds = r.json()["credentials"]
            assert len(creds) == 1
            assert creds[0]["label"] == "X"
            # No secret_ref / fingerprint
            assert "secret_ref" not in creds[0]
            assert "fingerprint_sha256" not in creds[0]


# ============================================================================
# 4. POST /api/credentials
# ============================================================================


class TestCreateCredential:
    def test_create_session_only_returns_201(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            cred = r.json()["credential"]
            assert cred["label"] == "X"
            assert cred["storage_mode"] == "session_only"
            assert cred["storage_status"] == "ready"
            assert cred["validation_status"] == "never_validated"
            assert "secret_ref" not in cred
            assert "fingerprint_sha256" not in cred

    def test_create_env_credential(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MY_KEY", "value")
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={
                    "label": "E",
                    "storage_mode": "env",
                    "env_var_name": "MY_KEY",
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            cred = r.json()["credential"]
            assert cred["masked_value"] == "ENV[MY_KEY]"

    def test_create_env_with_secret_value_rejected(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "env",
                    "secret_value": "sk-test-1234",
                    "env_var_name": "MY_KEY",
                },
                headers=_headers(),
            )
            assert r.status_code == 422

    def test_create_response_no_marker(self, tmp_path: Path) -> None:
        """Create with marker secret——response 不泄漏."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": SECRET_MARKER,
                },
                headers=_headers(),
            )
            assert r.status_code == 201
            assert SECRET_MARKER not in r.text


# ============================================================================
# 5. PATCH /api/credentials/{credential_id}
# ============================================================================


class TestUpdateLabel:
    def test_patch_updates_label(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "Old",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.patch(
                f"/api/credentials/{cred_id}",
                json={"label": "New"},
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["credential"]["label"] == "New"

    def test_patch_unknown_id_404(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.patch(
                "/api/credentials/cred-missing",
                json={"label": "X"},
                headers=_headers(),
            )
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "credential_not_found"


# ============================================================================
# 6. PUT /api/credentials/{credential_id}/secret
# ============================================================================


class TestRotateSecret:
    def test_rotate_resets_validation(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.put(
                f"/api/credentials/{cred_id}/secret",
                json={"secret_value": "sk-new-12345678901"},
                headers=_headers(),
            )
            assert r.status_code == 200
            cred = r.json()["credential"]
            assert cred["validation_status"] == "never_validated"

    def test_rotate_with_storage_mode_rejected(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.put(
                f"/api/credentials/{cred_id}/secret",
                json={"secret_value": "x", "storage_mode": "env"},
                headers=_headers(),
            )
            assert r.status_code == 422


# ============================================================================
# 7. DELETE /api/credentials/{credential_id}
# ============================================================================


class TestDeleteCredential:
    def test_delete_returns_200_with_warnings(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.delete(
                f"/api/credentials/{cred_id}", headers=_headers()
            )
            assert r.status_code == 200
            data = r.json()
            assert data["deleted"] is True
            assert data["credential_id"] == cred_id
            assert "warnings" in data

    def test_delete_unknown_404(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.delete(
                "/api/credentials/cred-missing", headers=_headers()
            )
            assert r.status_code == 404


# ============================================================================
# 8. POST /api/credentials/{credential_id}/validate
# ============================================================================


class TestValidateCredential:
    def test_anthropic_success(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path, strategy_result=_ok_result())
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-ant-test1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.post(
                f"/api/credentials/{cred_id}/validate",
                json={"provider_id": "anthropic"},
                headers=_headers(),
            )
            assert r.status_code == 200
            data = r.json()
            assert data["attempted"] is True
            assert data["valid"] is True
            assert data["validation_status"] == "valid"

    def test_anthropic_401_returns_invalid(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path, strategy_result=_auth_failed())
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-ant-test1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.post(
                f"/api/credentials/{cred_id}/validate",
                json={"provider_id": "anthropic"},
                headers=_headers(),
            )
            assert r.status_code == 200
            data = r.json()
            assert data["attempted"] is True
            assert data["valid"] is False
            assert data["error_code"] == "authentication_failed"
            assert data["validation_status"] == "invalid"

    def test_glm_returns_non_attempted_200(self, tmp_path: Path) -> None:
        """GLM=unsupported——non-attempted 200, not error."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.post(
                f"/api/credentials/{cred_id}/validate",
                json={"provider_id": "glm"},
                headers=_headers(),
            )
            assert r.status_code == 200
            data = r.json()
            assert data["attempted"] is False
            assert data["error_code"] == "validation_not_supported"

    def test_unknown_provider_non_attempted_200(self, tmp_path: Path) -> None:
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234567890",
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.post(
                f"/api/credentials/{cred_id}/validate",
                json={"provider_id": "unknown-provider"},
                headers=_headers(),
            )
            assert r.status_code == 200
            assert r.json()["error_code"] == "provider_not_supported"

    def test_validate_no_marker_in_response(self, tmp_path: Path) -> None:
        """Validate with marker secret——response 不泄漏."""
        app, _ = _build_test_app(tmp_path, strategy_result=_ok_result())
        with TestClient(app) as client:
            create = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": SECRET_MARKER,
                },
                headers=_headers(),
            )
            cred_id = create.json()["credential"]["credential_id"]

            r = client.post(
                f"/api/credentials/{cred_id}/validate",
                json={"provider_id": "anthropic"},
                headers=_headers(),
            )
            assert r.status_code == 200
            assert SECRET_MARKER not in r.text


# ============================================================================
# credential_id path validation
# ============================================================================


class TestCredentialIdPathValidation:
    def test_invalid_credential_id_format_rejected(self, tmp_path: Path) -> None:
        """Path 参数含非法字符——422."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            r = client.delete(
                "/api/credentials/../../etc/passwd", headers=_headers()
            )
            # FastAPI normalizes path——but pattern check should reject dots
            # If path normalization reaches endpoint, pattern check rejects
            # Either 404 / 422 / 400 is acceptable——not 200
            assert r.status_code != 200

    def test_oversized_credential_id_rejected(self, tmp_path: Path) -> None:
        """Path 参数超长——422."""
        app, _ = _build_test_app(tmp_path)
        with TestClient(app) as client:
            huge_id = "a" * 200
            r = client.delete(
                f"/api/credentials/{huge_id}", headers=_headers()
            )
            assert r.status_code == 422
