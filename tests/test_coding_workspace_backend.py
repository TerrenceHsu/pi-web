"""Offline contract tests for the provider-neutral coding sandbox layer."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_sandbox import (
    FakeSandboxBackend,
    SandboxBackend,
    SandboxBackendConfig,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxError,
    SandboxHandle,
    SandboxLimits,
    SandboxNetworkPolicy,
    SandboxOutputChunk,
    SandboxSecretRef,
    SandboxServiceConfig,
)


def _spec(*, operation_id: str = "op-test") -> SandboxCreateSpec:
    return SandboxCreateSpec(
        operation_id=operation_id,
        runtime_id="pi-agent-test-runtime",
    )


def test_service_config_is_disabled_and_networkless_by_default() -> None:
    config = SandboxServiceConfig()

    assert config.enabled is False
    assert config.backend is None
    assert config.network == SandboxNetworkPolicy(mode="none")
    assert config.workdir == "/workspace"
    assert config.max_concurrent_per_user == 1


def test_enabled_service_requires_explicit_backend() -> None:
    with pytest.raises(ValidationError, match="requires a backend"):
        SandboxServiceConfig(enabled=True)


def test_e2b_config_requires_exact_secret_reference_set() -> None:
    with pytest.raises(ValidationError, match="requires secret refs"):
        SandboxBackendConfig(provider="e2b", runtime_id="runtime")

    backend = SandboxBackendConfig(
        provider="e2b",
        runtime_id="runtime",
        secret_refs=(
            SandboxSecretRef(name="api_key", secret_ref="sandbox-e2b-prod"),
        ),
    )

    assert backend.get_secret_ref("api_key") == "sandbox-e2b-prod"
    assert backend.get_secret_ref("missing") is None
    assert "e2b_" not in repr(backend)


def test_modal_config_requires_two_named_secret_references() -> None:
    backend = SandboxBackendConfig(
        provider="modal",
        runtime_id="runtime",
        secret_refs=(
            SandboxSecretRef(name="token_id", secret_ref="sandbox-modal-id"),
            SandboxSecretRef(name="token_secret", secret_ref="sandbox-modal-secret"),
        ),
    )

    assert backend.get_secret_ref("token_id") == "sandbox-modal-id"
    with pytest.raises(ValidationError, match="must be unique"):
        SandboxBackendConfig(
            provider="modal",
            runtime_id="runtime",
            secret_refs=(
                SandboxSecretRef(name="token_id", secret_ref="one"),
                SandboxSecretRef(name="token_id", secret_ref="two"),
            ),
        )


def test_models_are_frozen_and_secret_refs_are_immutable() -> None:
    config = SandboxBackendConfig(provider="fake", runtime_id="runtime")

    assert isinstance(config.secret_refs, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        config.runtime_id = "changed"  # type: ignore[misc]


def test_secret_reference_rejects_common_raw_credential_prefixes() -> None:
    for value in ("e2b_actual-value", "ak-modal-id", "as-modal-secret"):
        with pytest.raises(ValidationError, match="looks like a credential"):
            SandboxSecretRef(name="api_key", secret_ref=value)


@pytest.mark.parametrize(
    "path",
    ["workspace", "/workspace/../secret", r"C:\workspace", "/workspace//src"],
)
def test_runtime_paths_reject_non_absolute_or_unnormalized_values(path: str) -> None:
    with pytest.raises(ValidationError):
        SandboxCreateSpec(
            operation_id="op",
            runtime_id="runtime",
            workdir=path,
        )


def test_network_policy_is_closed_by_default_and_allowlist_is_explicit() -> None:
    assert SandboxNetworkPolicy().mode == "none"
    with pytest.raises(ValidationError, match="cannot include"):
        SandboxNetworkPolicy(mode="none", allowed_domains=("pypi.org",))
    with pytest.raises(ValidationError, match="requires at least one"):
        SandboxNetworkPolicy(mode="allowlist")

    policy = SandboxNetworkPolicy(
        mode="allowlist",
        allowed_domains=("pypi.org", "files.pythonhosted.org"),
    )
    assert policy.allowed_domains == ("pypi.org", "files.pythonhosted.org")


def test_command_timeout_cannot_exceed_lifetime() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        SandboxLimits(lifetime_seconds=60, command_timeout_seconds=61)


def test_command_is_argv_only_and_result_has_explicit_terminal_semantics() -> None:
    command = SandboxCommand(
        command_id="cmd-1",
        argv=("pytest", "tests", "-q"),
    )
    result = SandboxCommandResult(
        command_id=command.command_id,
        exit_code=0,
        started_at_ms=10,
        finished_at_ms=20,
    )

    assert command.argv == ("pytest", "tests", "-q")
    assert result.succeeded is True
    with pytest.raises(ValidationError, match="must not include exit_code"):
        SandboxCommandResult(
            command_id="cmd-2",
            termination_reason="timed_out",
            exit_code=1,
            started_at_ms=10,
            finished_at_ms=20,
        )


def test_sandbox_error_uses_fixed_safe_text_and_retry_classification() -> None:
    authentication = SandboxError("authentication_failed", provider="e2b")
    unavailable = SandboxError("provider_unavailable", provider="e2b")

    assert str(authentication) == "Sandbox provider authentication failed."
    assert authentication.retryable is False
    assert unavailable.retryable is True
    assert "api_key" not in repr(authentication)


def test_fake_backend_conforms_to_runtime_protocol() -> None:
    backend: SandboxBackend = FakeSandboxBackend()

    assert isinstance(backend, SandboxBackend)
    assert backend.backend_name() == "fake"


@pytest.mark.asyncio
async def test_fake_backend_create_attach_and_idempotent_destroy() -> None:
    backend = FakeSandboxBackend(clock_ms=lambda: 1_000, id_factory=lambda: "fake-one")

    handle = await backend.create(_spec())

    assert handle.provider == "fake"
    assert handle.created_at_ms == 1_000
    assert handle.expires_at_ms == 1_801_000
    assert await backend.attach(handle) == handle
    assert (await backend.status(handle)).state == "ready"
    await backend.destroy(handle)
    await backend.destroy(handle)
    assert backend.destroyed_sandbox_ids == ["fake-one"]
    assert (await backend.status(handle)).state == "terminated"
    with pytest.raises(SandboxError) as exc_info:
        await backend.attach(handle)
    assert exc_info.value.code == "sandbox_not_found"


@pytest.mark.asyncio
async def test_fake_backend_upload_download_round_trip(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-transfer")
    handle = await backend.create(_spec())
    source = tmp_path / "input.tar.zst"
    source.write_bytes("沙箱内容".encode())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    uploaded = await backend.upload_file(
        handle,
        local_path=source,
        remote_path="/workspace/input.tar.zst",
        expected_sha256=digest,
    )
    destination = tmp_path / "nested" / "output.tar.zst"
    downloaded = await backend.download_file(
        handle,
        remote_path="/workspace/input.tar.zst",
        local_path=destination,
        expected_sha256=digest,
    )

    assert uploaded == downloaded
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_fake_backend_digest_mismatch_never_publishes_destination(
    tmp_path: Path,
) -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-mismatch")
    handle = await backend.create(_spec())
    source = tmp_path / "input.bin"
    source.write_bytes(b"content")

    with pytest.raises(SandboxError) as exc_info:
        await backend.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/input.bin",
            expected_sha256="0" * 64,
        )
    assert exc_info.value.code == "transfer_failed"

    destination = tmp_path / "must-not-exist.bin"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_fake_backend_streams_bounded_output_in_order() -> None:
    queued = SandboxCommandResult(
        command_id="queued",
        exit_code=2,
        stdout="abcd",
        stderr="efgh",
        started_at_ms=10,
        finished_at_ms=20,
    )
    backend = FakeSandboxBackend(
        id_factory=lambda: "fake-command",
        command_results=(queued,),
    )
    handle = await backend.create(_spec())
    chunks: list[SandboxOutputChunk] = []

    async def on_output(chunk: SandboxOutputChunk) -> None:
        chunks.append(chunk)

    result = await backend.execute(
        handle,
        SandboxCommand(
            command_id="cmd-real",
            argv=("pytest", "-q"),
            max_output_bytes=5,
        ),
        on_output=on_output,
    )

    assert result.command_id == "cmd-real"
    assert result.exit_code == 2
    assert result.stdout == "abcd"
    assert result.stderr == "e"
    assert result.output_truncated is True
    assert [(chunk.sequence, chunk.stream, chunk.text) for chunk in chunks] == [
        (0, "stdout", "abcd"),
        (1, "stderr", "e"),
    ]


@pytest.mark.asyncio
async def test_fake_backend_honors_pre_cancelled_signal() -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-cancel")
    handle = await backend.create(_spec())
    signal = asyncio.Event()
    signal.set()

    result = await backend.execute(
        handle,
        SandboxCommand(command_id="cmd-cancel", argv=("pytest",)),
        signal=signal,
    )

    assert result.termination_reason == "cancelled"
    assert result.exit_code is None
    assert result.succeeded is False


@pytest.mark.asyncio
async def test_fake_backend_rejects_tampered_handle() -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-handle")
    handle = await backend.create(_spec())
    tampered = SandboxHandle(
        provider=handle.provider,
        sandbox_id=handle.sandbox_id,
        operation_id="different-operation",
        runtime_id=handle.runtime_id,
        created_at_ms=handle.created_at_ms,
        expires_at_ms=handle.expires_at_ms,
    )

    with pytest.raises(SandboxError) as exc_info:
        await backend.attach(tampered)
    assert exc_info.value.code == "sandbox_not_found"
