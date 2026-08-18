"""Regression tests for persistent Keyring startup capability checks."""
from __future__ import annotations

import pytest

import pi_agent_core_py.secrets as secrets_module
from pi_agent_core_py.web.credentials import runtime as runtime_module
from scripts import dev_web_app

pytestmark = pytest.mark.asyncio


class _ProbeStore:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.probe_calls = 0

    async def probe_write_access(self) -> bool:
        self.probe_calls += 1
        return self.result


async def test_runtime_rejects_discovered_but_unwritable_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ProbeStore(False)
    monkeypatch.setattr(runtime_module, "OSKeyringSecretStore", lambda: store)

    assert await runtime_module._probe_keyring_available() is False
    assert store.probe_calls == 1


async def test_runtime_accepts_only_successful_write_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ProbeStore(True)
    monkeypatch.setattr(runtime_module, "OSKeyringSecretStore", lambda: store)

    assert await runtime_module._probe_keyring_available() is True
    assert store.probe_calls == 1


async def test_dev_launcher_refuses_unwritable_persistent_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ProbeStore(False)
    monkeypatch.setattr(secrets_module, "OSKeyringSecretStore", lambda: store)

    with pytest.raises(RuntimeError, match="Persistent Keyring preflight failed"):
        await dev_web_app._require_persistent_keyring("keyring")
    assert store.probe_calls == 1


async def test_dev_launcher_accepts_writable_persistent_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ProbeStore(True)
    monkeypatch.setattr(secrets_module, "OSKeyringSecretStore", lambda: store)

    await dev_web_app._require_persistent_keyring("auto")
    assert store.probe_calls == 1


async def test_dev_launcher_memory_mode_skips_keyring_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_store() -> _ProbeStore:
        raise AssertionError("memory mode must not construct Keyring")

    monkeypatch.setattr(secrets_module, "OSKeyringSecretStore", _unexpected_store)

    await dev_web_app._require_persistent_keyring("memory")


async def test_dev_launcher_rejects_unknown_backend() -> None:
    with pytest.raises(RuntimeError, match="PI_AGENT_SECRET_BACKEND"):
        await dev_web_app._require_persistent_keyring("sqlite")
