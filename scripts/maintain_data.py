"""Explicit offline maintenance CLI. Existing destinations are never overwritten."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from pi_agent_core_py.maintenance import (
    MaintenanceError,
    backup_data,
    installation_lock,
    restore_data,
    verify_backup,
)
from pi_agent_core_py.web.wiki.upgrade import upgrade_wiki


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backup", "verify", "restore", "wiki-upgrade"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument(
        "--offline", action="store_true", help="Confirm Web AND parser Worker stopped",
    )
    parser.add_argument(
        "--data-root", type=Path, help="Installation root for Wiki maintenance lock",
    )
    parser.add_argument("--mode", choices=["compatible", "rebuild-sources"], default="compatible")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = {"verified": True, "files": len(verify_backup(args.source)["files"])}
        else:
            if args.destination is None:
                parser.error("--destination is required")
            if args.command == "backup":
                result = backup_data(args.source, args.destination, offline=args.offline)
            elif args.command == "restore":
                result = restore_data(args.source, args.destination, offline=args.offline)
            else:
                if not args.offline or args.data_root is None:
                    parser.error("wiki-upgrade requires --offline and --data-root")
                if args.data_root.resolve() not in args.source.resolve().parents:
                    parser.error("Wiki source must be inside --data-root")
                with installation_lock(args.data_root):
                    result = asyncio.run(upgrade_wiki(
                        args.source, args.destination, mode=args.mode, offline=True,
                    ))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (MaintenanceError, OSError, sqlite3.Error, ValueError) as error:
        code = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
        print(json.dumps({"ok": False, "error_code": code, "partial_destination_may_exist": True}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
