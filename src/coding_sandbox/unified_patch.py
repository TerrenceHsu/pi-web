"""Small, deterministic unified-diff parser used inside disposable workspaces."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .workspace_models import SandboxWorkspaceError, validate_workspace_relative_path

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?(?:\r?\n)?$")
_MAX_PATCH_BYTES = 1024 * 1024
_MAX_FILES = 500
_MAX_HUNKS = 5000


@dataclass(frozen=True)
class UnifiedHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class UnifiedFilePatch:
    path: str
    is_new: bool
    is_delete: bool
    hunks: tuple[UnifiedHunk, ...]


def parse_unified_diff(value: str) -> tuple[UnifiedFilePatch, ...]:
    """Parse a bounded, rename-free unified diff without touching files."""
    if not value or len(value.encode("utf-8")) > _MAX_PATCH_BYTES or "\x00" in value:
        raise SandboxWorkspaceError("patch_invalid")
    lines = value.splitlines(keepends=True)
    patches: list[UnifiedFilePatch] = []
    index = 0
    total_hunks = 0
    seen_paths: set[str] = set()

    while index < len(lines):
        if not lines[index].startswith("--- "):
            if lines[index].startswith(("diff --git ", "index ")) or not lines[index].strip():
                index += 1
                continue
            raise SandboxWorkspaceError("patch_invalid")
        old_name = _header_path(lines[index], "--- ")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise SandboxWorkspaceError("patch_invalid")
        new_name = _header_path(lines[index], "+++ ")
        index += 1

        is_new = old_name == "/dev/null"
        is_delete = new_name == "/dev/null"
        if is_new and is_delete:
            raise SandboxWorkspaceError("patch_invalid")
        old_path = None if is_new else _normalize_header_path(old_name)
        new_path = None if is_delete else _normalize_header_path(new_name)
        if old_path is not None and new_path is not None and old_path != new_path:
            raise SandboxWorkspaceError("patch_invalid")
        path = new_path or old_path
        if path is None or path in seen_paths:
            raise SandboxWorkspaceError("patch_invalid")
        seen_paths.add(path)

        hunks: list[UnifiedHunk] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            match = _HUNK_HEADER.fullmatch(lines[index])
            if match is None:
                raise SandboxWorkspaceError("patch_invalid", relative_path=path)
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            index += 1
            body: list[str] = []
            consumed = 0
            produced = 0
            while consumed < old_count or produced < new_count:
                if index >= len(lines):
                    raise SandboxWorkspaceError("patch_invalid", relative_path=path)
                line = lines[index]
                if not line or line[0] not in {" ", "+", "-"}:
                    raise SandboxWorkspaceError("patch_invalid", relative_path=path)
                body.append(line)
                if line[0] in {" ", "-"}:
                    consumed += 1
                if line[0] in {" ", "+"}:
                    produced += 1
                if consumed > old_count or produced > new_count:
                    raise SandboxWorkspaceError("patch_invalid", relative_path=path)
                index += 1
            hunks.append(
                UnifiedHunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    lines=tuple(body),
                )
            )
            total_hunks += 1
            if total_hunks > _MAX_HUNKS:
                raise SandboxWorkspaceError("resource_limit")
        if not hunks:
            raise SandboxWorkspaceError("patch_invalid", relative_path=path)
        patches.append(
            UnifiedFilePatch(
                path=path,
                is_new=is_new,
                is_delete=is_delete,
                hunks=tuple(hunks),
            )
        )
        if len(patches) > _MAX_FILES:
            raise SandboxWorkspaceError("resource_limit")
    if not patches:
        raise SandboxWorkspaceError("patch_invalid")
    return tuple(patches)


def apply_file_patch(original: str | None, patch: UnifiedFilePatch) -> str | None:
    """Apply one previously parsed patch with exact context matching."""
    if patch.is_new:
        if original is not None:
            raise SandboxWorkspaceError("patch_conflict", relative_path=patch.path)
        source: list[str] = []
    else:
        if original is None:
            raise SandboxWorkspaceError("patch_conflict", relative_path=patch.path)
        source = original.splitlines(keepends=True)

    output: list[str] = []
    cursor = 0
    for hunk in patch.hunks:
        old_index = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        new_index = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
        unchanged_count = old_index - cursor
        if (
            old_index < cursor
            or old_index > len(source)
            or new_index != len(output) + unchanged_count
        ):
            raise SandboxWorkspaceError("patch_conflict", relative_path=patch.path)
        output.extend(source[cursor:old_index])
        cursor = old_index
        consumed = 0
        produced = 0
        for line in hunk.lines:
            marker = line[0]
            content = line[1:]
            if marker in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != content:
                    raise SandboxWorkspaceError("patch_conflict", relative_path=patch.path)
                cursor += 1
                consumed += 1
            if marker in {" ", "+"}:
                output.append(content)
                produced += 1
        if consumed != hunk.old_count or produced != hunk.new_count:
            raise SandboxWorkspaceError("patch_invalid", relative_path=patch.path)
    output.extend(source[cursor:])
    result = "".join(output)
    if patch.is_delete:
        if result:
            raise SandboxWorkspaceError("patch_conflict", relative_path=patch.path)
        return None
    return result


def _header_path(line: str, prefix: str) -> str:
    value = line[len(prefix) :].rstrip("\r\n")
    if not value or "\t" in value or value != value.strip():
        raise SandboxWorkspaceError("patch_invalid")
    return value


def _normalize_header_path(value: str) -> str:
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return validate_workspace_relative_path(value)


__all__ = [
    "UnifiedFilePatch",
    "UnifiedHunk",
    "apply_file_patch",
    "parse_unified_diff",
]
