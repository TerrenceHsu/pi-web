"""Bounded, reference-aware cache sweeping; never touches Workspace or publisher journals."""

from __future__ import annotations

import os
import time
from pathlib import Path

from coding_sandbox.docker_transport import require_plain_path


def sweep_cache(
    root: Path, protected: set[Path], *, ttl_seconds: int = 30 * 86400
) -> dict[str, int]:
    if not root.exists():
        if root.is_symlink():
            raise OSError("cache_root_link")
        return {"bytes": 0, "removed_bytes": 0, "removed_files": 0}
    require_plain_path(root, directory=True)
    root = root.resolve()
    total = removed = count = scanned = 0
    cutoff = time.time() - ttl_seconds
    # Only internal execution snapshots/transfers/artifacts, not signing keys,
    # transaction receipts, user uploads or materialized business workspaces.
    for relative in (
        "snapshots",
        "files",
        "operation-files",
        "execution/snapshots",
        "execution/files",
    ):
        directory = root / relative
        if not directory.exists():
            continue
        require_plain_path(directory, directory=True)
        for base, dirs, files in os.walk(directory, followlinks=False):
            parent = Path(base)
            require_plain_path(parent, directory=True)
            for name in tuple(dirs):
                require_plain_path(parent / name, directory=True)
            for name in files:
                scanned += 1
                if scanned > 50_000:
                    raise OSError("cache_file_limit")
                path = parent / name
                require_plain_path(path)
                path = path.resolve(strict=True)
                path.relative_to(root)
                stat = path.stat()
                total += stat.st_size
                if path in protected or stat.st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += stat.st_size
                count += 1
    return {"bytes": total - removed, "removed_bytes": removed, "removed_files": count}
