"""Local validation of explicit provider endpoints; never resolves DNS or secrets."""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

ApiStyle = Literal["anthropic_compatible", "openai_compatible"]
GENERIC_PROVIDER_IDS = frozenset({"openai_compatible", "anthropic_compatible"})


def capability_provider_id(provider_id: str, api_style: str | None, base_url: str | None) -> str:
    """Keep legacy limits, but isolate user overrides by effective custom endpoint."""
    from .registry import get_provider_definition

    definition = get_provider_definition(provider_id)
    if definition is not None and provider_id not in GENERIC_PROVIDER_IDS:
        if (
            (api_style or definition.api_style) == definition.api_style
            and (base_url or definition.default_base_url).rstrip("/")
            == definition.default_base_url.rstrip("/")
        ):
            return provider_id
    if base_url is None and api_style is None:
        return provider_id
    material = f"{api_style}\n{base_url}".encode()
    return "custom:" + hashlib.sha256(material).hexdigest()[:48]


class InvalidProviderEndpointError(ValueError):
    """Safe configuration error which never includes the submitted address."""


def normalize_api_style(value: str | None) -> ApiStyle | None:
    if value is None or value in ("anthropic_compatible", "openai_compatible"):
        return cast(ApiStyle | None, value)
    raise InvalidProviderEndpointError("Provider API protocol is invalid.")


def normalize_base_url(value: str | None) -> str | None:
    """HTTPS, or explicit local HTTP. Reject credentials and URL side channels."""
    if value is None:
        return None
    message = "Provider endpoint must be HTTPS or a loopback HTTP address without credentials."
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise InvalidProviderEndpointError(message)
    if not value or len(value) > 2048 or "\\" in value or "?" in value or "#" in value:
        raise InvalidProviderEndpointError(message)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.netloc
            or parsed.port == 0
        ):
            raise ValueError
        local = host.lower() == "localhost"
        if not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if parsed.scheme == "http" and not local:
            raise ValueError
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise InvalidProviderEndpointError(message) from None
