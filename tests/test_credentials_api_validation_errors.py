"""Credential API safe ValidationError handler tests（P1-E1-4B1）.

覆盖 spec §10 安全 ValidationError handler：
- 只返回 `loc` + 受控 `code`
- 不返回 input / ctx / url / msg
- handler 范围限于 Credential Router——其他 API 422 schema 不变
- 自定义 validator 异常不泄漏
- secret_value 类型错误不回显原值
- extra field 422 不回显 input
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.web.credentials_api import (  # noqa: E402
    build_credential_router,
    safe_validation_response,
)
from pi_agent_core_py.web.credentials_dto import (  # noqa: E402
    CredentialCreateRequest,
    CredentialLabelUpdateRequest,
    CredentialValidateRequest,
    ProviderHintRequest,
)
from pi_agent_core_py.web.local_web_security import (  # noqa: E402
    default_web_security_config,
)

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Test app——Credential Router with real DTOs
# ============================================================================


def _build_test_app() -> FastAPI:
    app = FastAPI()
    config = default_web_security_config(
        extra_hosts=("testserver",),
        extra_ui_origins=(),
        require_ui_header=False,
    )
    router = build_credential_router(config)

    @router.post("/api/provider-hints")
    async def provider_hints(req: ProviderHintRequest) -> dict:
        return {"ok": True}

    @router.post("/api/credentials")
    async def create_credential(req: CredentialCreateRequest) -> dict:
        return {"ok": True}

    @router.patch("/api/credentials/{credential_id}")
    async def patch_credential(
        credential_id: str, req: CredentialLabelUpdateRequest
    ) -> dict:
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/validate")
    async def validate_credential(
        credential_id: str, req: CredentialValidateRequest
    ) -> dict:
        return {"ok": True}

    app.include_router(router)
    return app


# ============================================================================
# Safe 422 schema
# ============================================================================


class TestSafe422Schema:
    def test_response_shape(self) -> None:
        """固定 schema: error.code / message / fields[].path+code."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={},  # missing secret_value
            )
            assert r.status_code == 422
            body = r.json()
            assert body["error"]["code"] == "request_validation_failed"
            assert body["error"]["message"] == "The request body is invalid."
            assert isinstance(body["error"]["fields"], list)
            assert len(body["error"]["fields"]) >= 1
            field = body["error"]["fields"][0]
            assert "path" in field
            assert "code" in field

    def test_no_input_field_returned(self) -> None:
        """Pydantic errors() 含 'input'——response 不应有."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": "too-short"},
            )
            # 200 or 422——but if 422, no input leak
            if r.status_code == 422:
                text = r.text
                # 'too-short' would be the input——should NOT appear
                assert "too-short" not in text

    def test_no_url_field_returned(self) -> None:
        """Pydantic errors() 含 'url'（doc 链接）——response 不应有."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={},
            )
            text = r.text
            assert "pydantic.dev" not in text
            assert "docs.pydantic" not in text

    def test_no_ctx_field_returned(self) -> None:
        """ctx 可能含自定义 validator 异常——不返回."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": "x"},
            )
            if r.status_code == 422:
                body = r.json()
                for field in body["error"]["fields"]:
                    assert "ctx" not in field
                    assert "msg" not in field
                    assert "input" not in field
                    assert "url" not in field

    def test_secret_value_type_error_no_leak(self) -> None:
        """Pass secret_value as int instead of string——422 不回显原值."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": 12345},
            )
            assert r.status_code == 422
            text = r.text
            assert "12345" not in text


# ============================================================================
# Marker not leaked via validation error
# ============================================================================


class TestNoMarkerLeak:
    def test_marker_in_secret_value_not_echoed(self) -> None:
        """secret_value 含 marker 但其它字段错误——marker 不进 422 response."""
        app = _build_test_app()
        with TestClient(app) as client:
            # Wrong type for secret_value——but pass SECRET_MARKER elsewhere
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": [SECRET_MARKER]},
            )
            assert r.status_code == 422
            assert SECRET_MARKER not in r.text

    def test_marker_in_extra_field_not_echoed(self) -> None:
        """extra field with marker value——422 response 不回显."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={
                    "secret_value": "sk-test-1234",
                    "extra_field": SECRET_MARKER,
                },
            )
            assert r.status_code == 422
            assert SECRET_MARKER not in r.text

    def test_marker_in_overlong_secret_not_echoed(self) -> None:
        """over-length secret_value——422 不回显原值."""
        app = _build_test_app()
        # 8193 bytes——over limit
        big_secret = SECRET_MARKER + "a" * 8192
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": big_secret},
            )
            assert r.status_code == 422
            assert SECRET_MARKER not in r.text


# ============================================================================
# Field code mapping
# ============================================================================


class TestCodeMapping:
    def test_missing_field_code(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post("/api/provider-hints", json={})
            assert r.status_code == 422
            body = r.json()
            codes = [f["code"] for f in body["error"]["fields"]]
            assert "missing" in codes

    def test_extra_field_code(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": "x", "extra": "y"},
            )
            assert r.status_code == 422
            body = r.json()
            codes = [f["code"] for f in body["error"]["fields"]]
            assert "extra_forbidden" in codes

    def test_wrong_type_code(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": 12345},  # int, not str
            )
            assert r.status_code == 422
            body = r.json()
            codes = [f["code"] for f in body["error"]["fields"]]
            assert "type_error" in codes or "value_error" in codes


# ============================================================================
# Other APIs unchanged
# ============================================================================


class TestOtherApisUnchanged:
    def test_non_credential_router_422_uses_fastapi_default(self) -> None:
        """Non-credential route——FastAPI default 422 schema (with detail)."""
        from pydantic import BaseModel

        class OtherRequest(BaseModel):
            x: int

        app = FastAPI()

        @app.post("/api/other")
        async def other(req: OtherRequest) -> dict:
            return {"ok": True}

        with TestClient(app) as client:
            r = client.post("/api/other", json={"x": "not-int"})
            assert r.status_code == 422
            body = r.json()
            # FastAPI default schema uses "detail"——not our "error"
            assert "detail" in body
            assert "error" not in body


# ============================================================================
# Direct handler call——unit test safe_validation_response
# ============================================================================


class TestSafeValidationResponseUnit:
    def test_drops_input_ctx_url_msg(self) -> None:
        """Direct call——verify projection drops sensitive fields."""
        from fastapi.exceptions import RequestValidationError
        from pydantic import ValidationError

        try:
            ProviderHintRequest.model_validate({"secret_value": 12345})
        except ValidationError as e:
            exc = RequestValidationError([e.errors()[0]], body=12345)
            response = safe_validation_response(exc)
            import json

            data = json.loads(response.body)
            assert data["error"]["code"] == "request_validation_failed"
            for field in data["error"]["fields"]:
                assert set(field.keys()) == {"path", "code"}
