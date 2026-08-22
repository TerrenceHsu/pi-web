"""write_file tool — create a UTF-8 file in the active Session folder."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..messages import TextContent
from . import AgentTool, ToolResult, ToolUpdateCallback
from .view_file import _classify_format

if TYPE_CHECKING:
    from ..web.files import WorkspaceStore


class WriteFileTool(AgentTool):
    """Create a new managed file without accepting a physical filesystem path."""

    name = "write_file"
    label = "Write File"
    description = (
        "Create a new UTF-8 text file in the current conversation session folder. "
        "The file is isolated to the current session and becomes available to "
        "list_files, view_file, download, and the user interface. This tool always "
        "creates a new managed file and never overwrites an existing file. Code "
        "files are automatically placed below the logical scripts/ directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "File name only, for example report.md or data.csv.",
                "minLength": 1,
                "maxLength": 255,
            },
            "content": {
                "type": "string",
                "description": "Complete UTF-8 text content for the new file.",
            },
            "folder": {
                "type": "string",
                "description": (
                    "Optional relative folder in the Session tree, for example "
                    "outputs or reports/2026. For code this folder is below scripts/. "
                    "Absolute paths and .. are forbidden."
                ),
                "maxLength": 512,
            },
        },
        "required": ["filename", "content"],
        "additionalProperties": False,
    }
    execution_mode = "sequential"

    def __init__(
        self,
        *,
        file_store: WorkspaceStore,
        session_id_getter: Callable[[], str | None],
    ) -> None:
        self._file_store = file_store
        self._session_id_getter = session_id_getter

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        filename = args.get("filename")
        content = args.get("content")
        folder = args.get("folder")
        if not isinstance(filename, str) or not filename.strip():
            return _error(
                tool_call_id,
                "filename is required and must be a non-empty string.",
                error_type="InvalidArguments",
            )
        if not isinstance(content, str):
            return _error(
                tool_call_id,
                "content is required and must be a string.",
                error_type="InvalidArguments",
            )
        if folder is not None and not isinstance(folder, str):
            return _error(
                tool_call_id,
                "folder must be a string when provided.",
                error_type="InvalidArguments",
            )

        session_id = self._session_id_getter()
        if not session_id:
            return _error(
                tool_call_id,
                "write_file failed: no active session_id.",
                error_type="NoActiveSession",
            )

        from ..web.files import (
            FileStoreError,
            FileTooLargeError,
            SessionStorageLimitError,
            UnsafeFilenameError,
        )

        try:
            ref = await self._file_store.write_text(
                session_id,
                filename,
                content,
                folder=folder,
                origin="agent",
            )
            workspace = await self._file_store.get_workspace_state(session_id)
        except FileTooLargeError as exc:
            return _error(
                tool_call_id,
                str(exc),
                error_type="FileTooLarge",
            )
        except SessionStorageLimitError as exc:
            return _error(
                tool_call_id,
                str(exc),
                error_type="SessionStorageLimit",
            )
        except UnsafeFilenameError as exc:
            return _error(
                tool_call_id,
                str(exc),
                error_type="UnsafeFilename",
            )
        except FileStoreError as exc:
            return _error(
                tool_call_id,
                f"write_file failed: {exc}",
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            return _error(
                tool_call_id,
                f"write_file failed: {type(exc).__name__}",
                error_type=type(exc).__name__,
            )

        file_format = _classify_format(ref.name, ref.mime)
        details = {
            "file_id": ref.id,
            "name": ref.name,
            "logical_path": ref.logical_path,
            "origin": ref.origin,
            "purpose": ref.purpose,
            "mime": ref.mime,
            "format": file_format,
            "size": ref.size,
            "sha256": ref.sha256,
            "session_id": session_id,
            "workspace_revision": workspace.revision,
        }
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(
                text=(
                    f"Created {ref.logical_path} in the current session folder "
                    f"({ref.size} bytes, id={ref.id})."
                ),
            )],
            details=details,
        )


def _error(
    tool_call_id: str,
    message: str,
    *,
    error_type: str,
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        name=WriteFileTool.name,
        content=[TextContent(text=message)],
        is_error=True,
        details={"error_type": error_type},
    )


def create_write_file_tool(
    *,
    file_store: WorkspaceStore,
    session_id_getter: Callable[[], str | None],
) -> WriteFileTool:
    return WriteFileTool(
        file_store=file_store,
        session_id_getter=session_id_getter,
    )


__all__ = ["WriteFileTool", "create_write_file_tool"]
