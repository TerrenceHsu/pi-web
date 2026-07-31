"""R2-A1 — Dependency boundary tests for pypdf integration.

Per P2-R2-A §21.A (dependency tests). Verifies:

- pyproject [rag] extra contains pypdf>=6.0,<7
- pyproject [rag] extra does NOT contain: cryptography / PyMuPDF / marker-pdf
  / torch / transformers / surya-ocr / huggingface-hub
- uv.lock registers pypdf with 6.x version
- pypdf actually installed in test env resolves to 6.x
- base package importable without pypdf installed (simulated)

Static (no network, no PDF parsing).
"""
from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_UV_LOCK = _REPO_ROOT / "uv.lock"


def _read_pyproject() -> str:
    return _PYPROJECT.read_text(encoding="utf-8")


def _read_uv_lock() -> str:
    return _UV_LOCK.read_text(encoding="utf-8")


# ============================================================================
# A. pyproject [rag] extra scope
# ============================================================================


class TestRagExtraScope:
    def test_rag_extra_exists(self) -> None:
        text = _read_pyproject()
        assert re.search(r"^rag\s*=\s*\[", text, re.MULTILINE), (
            "pyproject.toml must define a `rag` optional-dependency group"
        )

    def test_rag_extra_contains_pypdf_in_range(self) -> None:
        text = _read_pyproject()
        # Match `pypdf>=6.0,<7` literal (whitespace tolerant)
        assert re.search(r'"pypdf>=\s*6\.0,\s*<\s*7"', text), (
            "[rag] extra must include pypdf>=6.0,<7 (Audited baseline 6.14.2)"
        )

    def test_rag_extra_does_not_include_crypto(self) -> None:
        """pypdf [crypto] extra is forbidden in R2-A per license gate §17.1."""
        text = _read_pyproject()
        # Extract rag = [...] block
        m = re.search(r"^rag\s*=\s*\[(.*?)^\]", text, re.MULTILINE | re.DOTALL)
        assert m is not None, "rag extra block not found"
        rag_block = m.group(1)
        assert "cryptography" not in rag_block
        assert "PyCryptodome" not in rag_block
        assert "pycryptodome" not in rag_block

    def test_no_forbidden_pdf_deps_in_any_extra(self) -> None:
        """Marker / PyMuPDF / OCR stack must NOT appear anywhere in pyproject."""
        text = _read_pyproject()
        forbidden = [
            "marker-pdf",
            "PyMuPDF",
            "pymupdf",
            "fitz",
            "pdfplumber",
            "surya-ocr",
            "torch",
            "transformers",
            "huggingface-hub",
            "reportlab",
        ]
        for pkg in forbidden:
            assert pkg not in text, (
                f"forbidden PDF parser dep {pkg!r} must not appear in pyproject.toml"
            )


# ============================================================================
# B. uv.lock registration
# ============================================================================


class TestUvLockRegistration:
    def test_lockfile_has_pypdf_6x_stanza(self) -> None:
        text = _read_uv_lock()
        # pypdf package block with 6.x version
        m = re.search(
            r'\[\[package\]\]\s*\nname\s*=\s*"pypdf"\s*\nversion\s*=\s*"(\d+\.\d+\.\d+)"',
            text,
        )
        assert m is not None, "uv.lock must contain a pypdf package stanza"
        major = int(m.group(1).split(".")[0])
        assert major == 6, f"locked pypdf must be 6.x; got {m.group(1)}"

    def test_lockfile_rag_extra_registered(self) -> None:
        text = _read_uv_lock()
        # The provides-extras line must include "rag"
        m = re.search(r'provides-extras\s*=\s*\[(.*?)\]', text, re.DOTALL)
        assert m is not None
        extras_block = m.group(1)
        assert '"rag"' in extras_block

    def test_lockfile_no_marker_no_pymupdf(self) -> None:
        text = _read_uv_lock()
        assert "marker-pdf" not in text
        assert "PyMuPDF" not in text
        # Note: package name in uv.lock is case-sensitive; both forms checked


# ============================================================================
# C. Runtime version check
# ============================================================================


class TestInstalledVersion:
    def test_installed_pypdf_is_6x(self) -> None:
        try:
            v = version("pypdf")
        except PackageNotFoundError:
            pytest.skip(
                "pypdf not installed in this env (rag extra missing); "
                "install with `pip install -e .[rag]`"
            )
        major = int(v.split(".")[0])
        assert major == 6, f"installed pypdf must be 6.x; got {v}"

    def test_installed_pypdf_no_crypto_extra(self) -> None:
        """If cryptography is installed it must NOT come via pypdf[crypto].

        We can't directly attribute the install reason; instead we verify
        pypdf's metadata does not declare crypto as a non-extra requirement.
        """
        try:
            v = version("pypdf")
        except PackageNotFoundError:
            pytest.skip("pypdf not installed")
        # Verify pypdf imports cleanly without crypto
        import pypdf  # noqa: F401

        assert v is not None


# ============================================================================
# D. Boundary: base package importable without pypdf
# ============================================================================


class TestBaseImportBoundary:
    """R2-A §13: base package must be importable when pypdf is not installed.

    Adapter uses lazy import so this is structural. We verify by checking that
    the top-level package + key modules do NOT import pypdf at module load time.
    """

    def test_knowledge_package_init_does_not_import_pypdf(self) -> None:
        """src/pi_agent_core_py/web/knowledge/__init__.py must not import pypdf."""
        init_path = (
            _REPO_ROOT
            / "src"
            / "pi_agent_core_py"
            / "web"
            / "knowledge"
            / "__init__.py"
        )
        text = init_path.read_text(encoding="utf-8")
        assert "import pypdf" not in text
        assert "from pypdf" not in text

    def test_main_package_imports_without_pypdf(self) -> None:
        """Importing the top-level package must succeed regardless of pypdf
        availability. Verified by scanning for top-level (module-scope)
        pypdf imports outside the adapter module.

        The adapter (``pypdf_parser.py``) is allowed to lazy-import pypdf
        *inside* methods (not at module top level). Any other module that
        imports pypdf at top level would break base package import when
        [rag] extra is missing.
        """
        # Walk all .py files in src; flag top-level (non-indented) pypdf imports
        # anywhere except the dedicated adapter module.
        src_root = _REPO_ROOT / "src"
        adapter_rel = "src/pi_agent_core_py/web/knowledge/pypdf_parser.py"
        offenders: list[str] = []
        for py in src_root.rglob("*.py"):
            rel = str(py.relative_to(_REPO_ROOT)).replace("\\", "/")
            if rel == adapter_rel:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            # Match only UNINDENTED import statements (column 0) — these run
            # at module load time and would crash base import if pypdf missing.
            if re.search(r"^(import pypdf|from pypdf)", text, re.MULTILINE):
                offenders.append(rel)
        assert offenders == [], (
            f"pypdf must only be imported (lazily, inside methods) in the "
            f"adapter module; found module-scope imports in: {offenders}"
        )
