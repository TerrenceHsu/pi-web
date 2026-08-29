"""Trusted, revision-bound summaries of the published Workspace code tree."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from .store import (
    ARCHITECTURE_PATH,
    CODE_FLOW_PATH,
    VALIDATION_PATH,
    WorkspaceMaterialization,
    WorkspaceMaterializationEntry,
    WorkspacePublishPolicyError,
    WorkspaceState,
    WorkspaceStore,
    is_code_workspace_path,
)

CodeContinuityTrigger: TypeAlias = Literal[
    "user_upload",
    "user_delete",
    "workspace_published",
    "recovery",
]

CODE_CONTINUITY_SCHEMA = "pi-agent-code-continuity-document/v1"
_MAX_FILES = 200
_MAX_FILE_CHARS = 64_000
_MAX_TOTAL_CHARS = 512_000
_ENTRYPOINT_NAMES = frozenset(
    {
        "app.py",
        "main.py",
        "manage.py",
        "server.py",
        "cli.py",
        "index.js",
        "index.ts",
        "main.js",
        "main.ts",
        "package.json",
        "cargo.toml",
        "go.mod",
    }
)
_LANGUAGE_BY_SUFFIX = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
}
_JS_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^;]*?\s+from\s+)?|require\s*\()"
    r"['\"]([^'\"]+)['\"]"
)
_JS_SYMBOL_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
)


class CodeContinuityError(RuntimeError):
    """Stable error raised while stale code summaries remain retryable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CodeFileSummary:
    path: str
    language: str
    size: int
    sha256: str
    entrypoint: bool
    imports: tuple[str, ...]
    symbols: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class CodeContinuityRefreshResult:
    workspace: WorkspaceState
    source_workspace_revision: int
    code_source_sha256: str
    changed_paths: tuple[str, ...]


class CodeContinuityService:
    """Render fixed summaries from verified bytes without arbitrary Agent writes."""

    def __init__(self, store: WorkspaceStore, *, staging_root: Path) -> None:
        if not staging_root.is_absolute():
            raise ValueError("code continuity staging root must be absolute")
        self._store = store
        self._staging_root = staging_root.resolve(strict=False)
        self._locks: dict[str, asyncio.Lock] = {}

    async def refresh(
        self,
        session_id: str,
        *,
        trigger: CodeContinuityTrigger,
        changed_paths: tuple[str, ...] = (),
        deleted_paths: tuple[str, ...] = (),
        validation: dict[str, Any] | None = None,
    ) -> CodeContinuityRefreshResult | None:
        """Refresh all summaries, then atomically expose their freshness marker."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            state = await self._store.get_workspace_state(session_id)
            if state.code_continuity.status == "not_initialized":
                has_existing_code = any(
                    is_code_workspace_path(ref.logical_path)
                    for ref in await self._store.list_session(session_id)
                )
                if not has_existing_code:
                    return None
                state = await self._store.initialize_code_continuity_stale(
                    session_id,
                    expected_workspace_revision=state.revision,
                )
            code_revision = state.code_continuity.latest_code_workspace_revision
            if code_revision is None or not state.code_continuity.stale:
                return None

            materialized_root = self._staging_root / f"code-{uuid4().hex}"
            try:
                materialization = await self._store.materialize_workspace_revision(
                    session_id,
                    materialized_root,
                    expected_workspace_revision=state.revision,
                )
                summaries, omitted = await asyncio.to_thread(
                    _summarize_code_tree,
                    materialization,
                )
                source_sha256 = code_source_sha256(materialization.entries)
                evidence_id = _validation_evidence_id(validation)
                documents = _render_documents(
                    summaries,
                    omitted=omitted,
                    source_workspace_revision=materialization.revision,
                    code_source_sha256=source_sha256,
                    trigger=trigger,
                    changed_paths=tuple(
                        path for path in changed_paths if is_code_workspace_path(path)
                    ),
                    deleted_paths=tuple(
                        path for path in deleted_paths if is_code_workspace_path(path)
                    ),
                    validation=validation,
                )
                written: list[str] = []
                for logical_path, content in documents:
                    if await self._upsert_document(session_id, logical_path, content):
                        written.append(logical_path)
                current = await self._store.mark_code_continuity_current(
                    session_id,
                    expected_code_workspace_revision=code_revision,
                    code_source_sha256=source_sha256,
                    trigger=trigger,
                    validation_evidence_id=evidence_id,
                )
                return CodeContinuityRefreshResult(
                    workspace=current,
                    source_workspace_revision=materialization.revision,
                    code_source_sha256=source_sha256,
                    changed_paths=tuple(written),
                )
            except CodeContinuityError:
                raise
            except Exception as exc:
                await self._store.mark_code_continuity_failed(
                    session_id,
                    expected_code_workspace_revision=code_revision,
                    error_code=type(exc).__name__,
                )
                raise CodeContinuityError("code_continuity_refresh_failed") from exc
            finally:
                await asyncio.to_thread(
                    shutil.rmtree,
                    materialized_root,
                    ignore_errors=True,
                )

    async def _upsert_document(
        self,
        session_id: str,
        logical_path: str,
        content: str,
    ) -> bool:
        existing = await self._store.get_by_logical_path(session_id, logical_path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if existing is not None and existing.sha256 == digest:
            return False
        if existing is not None:
            if existing.purpose != "workspace_documentation":
                raise WorkspacePublishPolicyError(
                    "fixed code continuity document has an unexpected owner"
                )
            await self._store.update_text(
                session_id,
                existing.id,
                content,
                expected_sha256=existing.sha256,
                origin="system",
                purpose="workspace_documentation",
            )
            return True

        path = PurePosixPath(logical_path)
        await self._store.write_text(
            session_id,
            path.name,
            content,
            content_type="text/markdown",
            folder=str(path.parent),
            origin="system",
            purpose="workspace_documentation",
            unique_logical_path=False,
        )
        return True


def code_source_sha256(entries: tuple[WorkspaceMaterializationEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        if not is_code_workspace_path(entry.logical_path):
            continue
        digest.update(entry.logical_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _summarize_code_tree(
    materialization: WorkspaceMaterialization,
) -> tuple[tuple[CodeFileSummary, ...], int]:
    entries = [
        entry
        for entry in materialization.entries
        if is_code_workspace_path(entry.logical_path)
    ]
    summaries: list[CodeFileSummary] = []
    remaining_chars = _MAX_TOTAL_CHARS
    for entry in entries[:_MAX_FILES]:
        path = materialization.root_path.joinpath(
            *PurePosixPath(entry.logical_path).parts
        )
        language = _language_for(entry.logical_path)
        imports: tuple[str, ...] = ()
        symbols: tuple[str, ...] = ()
        note: str | None = None
        if remaining_chars <= 0:
            note = "content analysis omitted by total character limit"
        else:
            character_limit = min(_MAX_FILE_CHARS, remaining_chars)
            try:
                with path.open("r", encoding="utf-8", errors="strict") as stream:
                    sample = stream.read(character_limit + 1)
            except UnicodeDecodeError:
                note = "binary or non-UTF-8 content; inventory only"
            else:
                text = sample[:character_limit]
                remaining_chars -= len(text)
                imports, symbols, note = _inspect_source(entry.logical_path, text)
                if len(sample) > character_limit:
                    note = "content analysis truncated"
        summaries.append(
            CodeFileSummary(
                path=entry.logical_path,
                language=language,
                size=entry.size,
                sha256=entry.sha256,
                entrypoint=_is_entrypoint(entry.logical_path),
                imports=imports,
                symbols=symbols,
                note=note,
            )
        )
    return tuple(summaries), max(0, len(entries) - len(summaries))


def _inspect_source(
    logical_path: str,
    text: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    suffix = PurePosixPath(logical_path).suffix.casefold()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return (), (), "Python syntax could not be parsed"
        python_imports: set[str] = set()
        python_symbols: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                python_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                python_imports.add("." * node.level + (node.module or ""))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                python_symbols.append(f"function {node.name}")
            elif isinstance(node, ast.ClassDef):
                python_symbols.append(f"class {node.name}")
        return tuple(sorted(python_imports)[:40]), tuple(python_symbols[:80]), None
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        js_imports = tuple(sorted(set(_JS_IMPORT_RE.findall(text)))[:40])
        js_symbols = tuple(f"symbol {name}" for name in _JS_SYMBOL_RE.findall(text)[:80])
        return js_imports, js_symbols, None
    return (), (), None


def _language_for(logical_path: str) -> str:
    path = PurePosixPath(logical_path)
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.casefold(), "Other")


def _is_entrypoint(logical_path: str) -> bool:
    path = PurePosixPath(logical_path)
    return path.name.casefold() in _ENTRYPOINT_NAMES or path.parts[-2:] == (
        "src",
        "lib.rs",
    )


def _validation_evidence_id(validation: dict[str, Any] | None) -> str | None:
    if validation is None:
        return None
    value = validation.get("evidence_id")
    return value if isinstance(value, str) else None


def _render_documents(
    summaries: tuple[CodeFileSummary, ...],
    *,
    omitted: int,
    source_workspace_revision: int,
    code_source_sha256: str,
    trigger: CodeContinuityTrigger,
    changed_paths: tuple[str, ...],
    deleted_paths: tuple[str, ...],
    validation: dict[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    marker = _render_marker(
        source_workspace_revision=source_workspace_revision,
        code_source_sha256=code_source_sha256,
        trigger=trigger,
        validation_evidence_id=_validation_evidence_id(validation),
    )
    languages = Counter(item.language for item in summaries)
    entrypoints = [item.path for item in summaries if item.entrypoint]
    dependency_files = [item for item in summaries if item.imports]
    architecture = [
        marker,
        "",
        "# Architecture",
        "",
        "This document is generated from verified, published Workspace bytes.",
        "",
        "## Code inventory",
        "",
        f"- Files analyzed: {len(summaries)}",
        f"- Files omitted by limit: {omitted}",
        "- Languages: "
        + (", ".join(f"{name} ({count})" for name, count in sorted(languages.items())) or "none"),
        "",
        "## Entrypoints",
        "",
        *([f"- `{path}`" for path in entrypoints] or ["- No conventional entrypoint detected."]),
        "",
        "## Module dependencies",
        "",
        *(
            [
                f"- `{item.path}` imports "
                + ", ".join(f"`{name}`" for name in item.imports)
                for item in dependency_files
            ]
            or ["- No supported import declarations detected."]
        ),
        "",
    ]

    flow = [
        marker,
        "",
        "# Code Flow",
        "",
        "## Triggering change",
        "",
        f"- Trigger: `{trigger}`",
        *(
            [f"- Changed: `{path}`" for path in changed_paths]
            or ["- Changed paths: unavailable or recovery run"]
        ),
        *([f"- Deleted: `{path}`" for path in deleted_paths]),
        "",
        "## File-level flow hints",
        "",
    ]
    for item in summaries:
        flow.extend(
            [
                f"### `{item.path}`",
                "",
                f"- Language: {item.language}",
                f"- SHA-256: `{item.sha256}`",
                f"- Entrypoint candidate: {'yes' if item.entrypoint else 'no'}",
                "- Imports: "
                + (", ".join(f"`{name}`" for name in item.imports) or "none detected"),
                "- Top-level symbols: "
                + (", ".join(item.symbols) or "none detected"),
                *([f"- Note: {item.note}"] if item.note else []),
                "",
            ]
        )
    if not summaries:
        flow.extend(["No files currently exist below `scripts/**`.", ""])

    validation_lines = [
        marker,
        "",
        "# Validation",
        "",
    ]
    validation_lines.extend(_render_validation(validation, trigger=trigger))
    return (
        (ARCHITECTURE_PATH, "\n".join(architecture)),
        (CODE_FLOW_PATH, "\n".join(flow)),
        (VALIDATION_PATH, "\n".join(validation_lines)),
    )


def _render_marker(
    *,
    source_workspace_revision: int,
    code_source_sha256: str,
    trigger: CodeContinuityTrigger,
    validation_evidence_id: str | None,
) -> str:
    return (
        "<!-- pi-code-continuity\n"
        f"schema: {CODE_CONTINUITY_SCHEMA}\n"
        f"source_workspace_revision: {source_workspace_revision}\n"
        f"code_source_sha256: {code_source_sha256}\n"
        f"trigger: {trigger}\n"
        f"validation_evidence_id: {validation_evidence_id or 'none'}\n"
        "-->"
    )


def _render_validation(
    validation: dict[str, Any] | None,
    *,
    trigger: CodeContinuityTrigger,
) -> list[str]:
    if validation is None:
        return [
            "## Status",
            "",
            "No Sandbox validation evidence is associated with this code revision.",
            f"The triggering event was `{trigger}`; this is not a claim that tests passed.",
            "",
        ]
    passed = validation.get("passed") is True
    evidence_id = _validation_evidence_id(validation) or "unknown"
    failure_code = validation.get("failure_code")
    checks = validation.get("checks")
    lines = [
        "## Status",
        "",
        f"- Evidence: `{evidence_id}`",
        f"- Result: `{'passed' if passed else 'failed'}`",
        f"- Failure code: `{failure_code or 'none'}`",
        "",
        "## Checks",
        "",
    ]
    if not isinstance(checks, list):
        return [*lines, "- Evidence contained no public check list.", ""]
    valid_entries = 0
    for item in checks:
        if not isinstance(item, dict):
            continue
        valid_entries += 1
        check_id = item.get("check_id")
        status = item.get("status")
        exit_code = item.get("exit_code")
        duration_ms = item.get("duration_ms")
        stdout_sha = item.get("captured_stdout_sha256")
        stderr_sha = item.get("captured_stderr_sha256")
        lines.append(
            f"- `{check_id or 'unknown'}`: status=`{status or 'unknown'}`, "
            f"exit={exit_code!r}, duration_ms={duration_ms!r}, "
            f"stdout_sha256=`{stdout_sha or 'none'}`, stderr_sha256=`{stderr_sha or 'none'}`"
        )
    if valid_entries == 0:
        lines.append("- No valid public check entries.")
    lines.append("")
    return lines


__all__ = [
    "CODE_CONTINUITY_SCHEMA",
    "CodeContinuityError",
    "CodeContinuityRefreshResult",
    "CodeContinuityService",
    "CodeContinuityTrigger",
    "CodeFileSummary",
    "code_source_sha256",
]
