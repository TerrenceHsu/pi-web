"""Offline safety tests for the interactive E2B configuration command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import configure_e2b_sandbox as cli


def test_cli_rejects_api_key_arguments_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "non-secret-argument-marker"
    monkeypatch.setattr(sys, "argv", ["configure_e2b_sandbox.py", "--api-key", marker])

    with pytest.raises(cli.ConfigurationCLIError) as captured:
        cli._parse_args()

    assert marker not in str(captured.value)


def test_hidden_key_input_requires_matching_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["non-secret-first", "non-secret-second"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

    with pytest.raises(cli.ConfigurationCLIError, match="confirmation did not match"):
        cli._read_api_key()


def test_hidden_key_input_returns_confirmed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = "e2b_" + "0" * 40
    answers = iter([placeholder, placeholder])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

    assert cli._read_api_key() == placeholder


@pytest.mark.parametrize(
    "invalid_value",
    [" leading", "trailing ", "embedded whitespace", "line\nbreak"],
)
def test_hidden_key_input_rejects_whitespace(
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: invalid_value)

    with pytest.raises(cli.ConfigurationCLIError, match="whitespace"):
        cli._read_api_key()


@pytest.mark.parametrize(
    "invalid_value",
    ["sk-" + "0" * 40, "e2b_not-hex", "e2b_ABCDEF"],
)
def test_hidden_key_input_rejects_non_e2b_key_shapes(
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: invalid_value)

    with pytest.raises(cli.ConfigurationCLIError, match="format is invalid"):
        cli._read_api_key()


@pytest.mark.asyncio
async def test_raw_sdk_diagnostic_reports_authentication_without_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(**_kwargs: object) -> object:
        raise cli.AuthenticationException("provider message must stay hidden")

    monkeypatch.setattr(cli.AsyncSandbox, "create", reject)

    assert (
        await cli._diagnose_raw_sdk_authentication("e2b_" + "0" * 40)
        == "raw_sdk_authentication_failed"
    )


@pytest.mark.asyncio
async def test_raw_sdk_diagnostic_always_kills_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sandbox:
        killed = False

        async def kill(self, **_kwargs: object) -> bool:
            self.killed = True
            return True

    sandbox = _Sandbox()

    async def create(**_kwargs: object) -> object:
        return sandbox

    monkeypatch.setattr(cli.AsyncSandbox, "create", create)

    assert (
        await cli._diagnose_raw_sdk_authentication("e2b_" + "0" * 40)
        == "raw_sdk_authenticated"
    )
    assert sandbox.killed is True


def test_workspace_discovery_requires_an_unambiguous_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    users = tmp_path / ".pi-agent-data" / "users"
    first = users / "user-a" / "workspace.sqlite"
    first.parent.mkdir(parents=True)
    first.touch()

    assert cli._resolve_workspace_database(None) == first.resolve()

    second = users / "user-b" / "workspace.sqlite"
    second.parent.mkdir(parents=True)
    second.touch()
    with pytest.raises(cli.ConfigurationCLIError, match="multiple workspace"):
        cli._resolve_workspace_database(None)
