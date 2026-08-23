"""The new page-centric store must not load or depend on legacy Chunk RAG."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_WIKI_ROOT = Path("src/pi_agent_core_py/web/wiki")


def test_wiki_package_has_no_legacy_knowledge_imports() -> None:
    for path in _WIKI_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            assert all("web.knowledge" not in name for name in names), path
        assert "knowledge_chunks" not in source
        assert "knowledge_chunks_fts" not in source


def test_importing_wiki_does_not_import_legacy_runtime() -> None:
    code = (
        "import sys; import pi_agent_core_py.web.wiki; "
        "assert not any(name.startswith('pi_agent_core_py.web.knowledge') "
        "for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
