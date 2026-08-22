"""Securely configure and probe E2B for one local pi-agent workspace.

The API key is read with ``getpass`` and is never accepted as an argument,
environment variable, log field, or persisted SQLite value. Only the existing
CredentialService may write it to the OS keyring.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hmac
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from e2b import AsyncSandbox, AuthenticationException  # noqa: E402

from coding_sandbox.admin import (  # noqa: E402
    SandboxAdminService,
    SandboxConnectionTestResult,
    SQLiteSandboxConfigStore,
)
from pi_agent_core_py.web.credentials.runtime import (  # noqa: E402
    build_credential_runtime_config,
    credential_runtime_context,
)
from pi_agent_core_py.web.credentials.service import (  # noqa: E402
    CreateCredentialCommand,
    CredentialService,
)
from pi_agent_core_py.web.local_web_security import WebSecurityConfig  # noqa: E402


class ConfigurationCLIError(RuntimeError):
    """Safe, secret-free operator error."""


_E2B_API_KEY_PATTERN = re.compile(r"\Ae2b_[0-9a-f]+\Z")


def _parse_args() -> argparse.Namespace:
    if any(
        argument == "--api-key" or argument.startswith("--api-key=")
        for argument in sys.argv[1:]
    ):
        raise ConfigurationCLIError(
            "API keys must be entered at the hidden prompt, never as arguments"
        )
    parser = argparse.ArgumentParser(
        description="Store an E2B key in Windows Keyring and run a safe connection probe.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--workspace-db",
        type=Path,
        help=(
            "Absolute workspace.sqlite path. Omit when .pi-agent-data contains "
            "exactly one user workspace."
        ),
    )
    parser.add_argument(
        "--template",
        default="base",
        help="E2B template name or ID (default: base).",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing E2B credential only after the new probe passes.",
    )
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Probe the existing configuration without prompting for a new API key.",
    )
    return parser.parse_args()


def _resolve_workspace_database(configured: Path | None) -> Path:
    if configured is not None:
        resolved = configured.expanduser().resolve(strict=False)
        if not resolved.is_absolute() or resolved.name != "workspace.sqlite":
            raise ConfigurationCLIError(
                "--workspace-db must be an absolute path ending in workspace.sqlite"
            )
        if not resolved.is_file():
            raise ConfigurationCLIError("the selected workspace database does not exist")
        return resolved

    candidates = sorted(
        path.resolve()
        for path in (REPOSITORY_ROOT / ".pi-agent-data" / "users").glob(
            "*/workspace.sqlite"
        )
        if path.is_file()
    )
    if not candidates:
        raise ConfigurationCLIError(
            "no workspace database found; log in once or pass --workspace-db"
        )
    if len(candidates) != 1:
        raise ConfigurationCLIError(
            "multiple workspace databases found; select one with --workspace-db"
        )
    return candidates[0]


def _read_api_key() -> str:
    first = getpass.getpass("E2B API key (hidden): ")
    second = getpass.getpass("Confirm E2B API key (hidden): ")
    if not first or first != second:
        raise ConfigurationCLIError("API key confirmation did not match")
    if first != first.strip() or any(char.isspace() for char in first):
        raise ConfigurationCLIError("API key must not contain whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in first):
        raise ConfigurationCLIError("API key must not contain control characters")
    if _E2B_API_KEY_PATTERN.fullmatch(first) is None:
        raise ConfigurationCLIError(
            "API key format is invalid; expected e2b_ followed by hexadecimal characters"
        )
    if len(first.encode("utf-8")) > 16 * 1024:
        raise ConfigurationCLIError("API key is too large")
    return first


def _print_probe(result: SandboxConnectionTestResult) -> None:
    print(f"probe_ok={str(result.ok).lower()}")
    print(f"runtime_id={result.runtime_id}")
    print(f"python_available={str(result.python_available).lower()}")
    print(f"duration_ms={result.duration_ms}")
    print(f"error_code={result.error_code or 'none'}")


async def _delete_credential_best_effort(
    credential_service: CredentialService,
    credential_id: str,
) -> bool:
    try:
        await credential_service.delete(credential_id)
    except Exception:
        return False
    return True


async def _diagnose_raw_sdk_authentication(api_key: str) -> str:
    sandbox: AsyncSandbox | None = None
    try:
        sandbox = await AsyncSandbox.create(
            template="base",
            timeout=30,
            secure=True,
            allow_internet_access=False,
            api_key=api_key,
            request_timeout=10,
        )
    except AuthenticationException:
        return "raw_sdk_authentication_failed"
    except Exception:
        return "raw_sdk_other_error"
    finally:
        if sandbox is not None:
            try:
                await sandbox.kill(request_timeout=10)
            except Exception:
                pass
    return "raw_sdk_authenticated"


async def _configure(args: argparse.Namespace) -> int:
    database_path = _resolve_workspace_database(args.workspace_db)
    template = args.template.strip()
    if not template or any(ord(char) < 32 for char in template):
        raise ConfigurationCLIError("template must be non-empty and contain no controls")

    credential_config = build_credential_runtime_config(
        database_path=database_path,
        secret_backend_mode="keyring",
        web_security=WebSecurityConfig(),
    )
    async with credential_runtime_context(credential_config) as credential_runtime:
        if not credential_runtime.readiness.keyring_available:
            raise ConfigurationCLIError(
                "Windows Keyring is unavailable from this terminal session"
            )

        sandbox_store = await SQLiteSandboxConfigStore.open(database_path)
        try:
            admin = SandboxAdminService(
                store=sandbox_store,
                credential_service=credential_runtime.service,
            )
            previous = await admin.get_config()
            previous_ids = tuple(
                credential_id
                for _, credential_id in previous.config.credential_references()
            )
            previous_credential_id = previous.config.credential_id

            if args.test_only or (previous_credential_id is not None and not args.replace):
                if previous_credential_id is None:
                    raise ConfigurationCLIError(
                        "no existing E2B configuration is available to test"
                    )
                result = await admin.test_connection()
                _print_probe(result)
                return 0 if result.ok else 2

            api_key = _read_api_key()
            created = None
            try:
                created = await credential_runtime.service.create(
                    CreateCredentialCommand(
                        label="E2B Coding Sandbox",
                        storage_mode="keyring",
                        secret_value=api_key,
                    )
                )
                resolved_api_key = await credential_runtime.service.resolve_secret_for_request(
                    created.record.id
                )
                try:
                    if not hmac.compare_digest(api_key, resolved_api_key):
                        raise ConfigurationCLIError(
                            "Windows Keyring round-trip verification failed"
                        )
                finally:
                    del resolved_api_key
            except BaseException:
                if created is not None:
                    await _delete_credential_best_effort(
                        credential_runtime.service,
                        created.record.id,
                    )
                raise
            finally:
                del api_key

            new_credential_id = created.record.id
            configured = None
            try:
                next_payload = previous.config.model_dump(mode="python")
                next_payload.update(
                    {
                        "enabled": True,
                        "provider": "e2b",
                        "runtime_id": template,
                        "credential_id": new_credential_id,
                    }
                )
                next_config = type(previous.config).model_validate(next_payload)
                configured = await admin.update_config(
                    next_config,
                    expected_revision=previous.revision,
                )
                result = await admin.test_connection()
                _print_probe(result)
                if result.error_code == "authentication_failed":
                    diagnostic_api_key = (
                        await credential_runtime.service.resolve_secret_for_request(
                            new_credential_id
                        )
                    )
                    try:
                        auth_diagnostic = await _diagnose_raw_sdk_authentication(
                            diagnostic_api_key
                        )
                    finally:
                        del diagnostic_api_key
                    print(f"auth_diagnostic={auth_diagnostic}")
                if not result.ok:
                    rolled_back = await admin.update_config(
                        previous.config,
                        expected_revision=configured.revision,
                    )
                    del rolled_back
                    if not await _delete_credential_best_effort(
                        credential_runtime.service,
                        new_credential_id,
                    ):
                        print("warning=new_credential_cleanup_failed")
                    return 2
            except BaseException:
                safe_to_delete_new_credential = configured is None
                if configured is not None:
                    try:
                        await admin.update_config(
                            previous.config,
                            expected_revision=configured.revision,
                        )
                        safe_to_delete_new_credential = True
                    except Exception:
                        safe_to_delete_new_credential = False
                if safe_to_delete_new_credential:
                    await _delete_credential_best_effort(
                        credential_runtime.service,
                        new_credential_id,
                    )
                raise

            obsolete_ids = tuple(
                credential_id
                for credential_id in previous_ids
                if credential_id != new_credential_id
            )
            cleanup_ok = all(
                [
                    await _delete_credential_best_effort(
                        credential_runtime.service,
                        credential_id,
                    )
                    for credential_id in obsolete_ids
                ]
            )
            if not cleanup_ok:
                print("warning=previous_credential_cleanup_failed")
            print(f"credential_id={new_credential_id}")
            print(f"config_revision={configured.revision}")
            return 0
        finally:
            await sandbox_store.close()


def main() -> None:
    try:
        exit_code = asyncio.run(_configure(_parse_args()))
    except KeyboardInterrupt:
        print("configuration_cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except ConfigurationCLIError as exc:
        print(f"configuration_error={exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"configuration_error_type={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
