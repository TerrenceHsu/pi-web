"""Architecture guardrails for the standalone Wiki parser contract package."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from wiki_parser import FakeParserImage, FakeParserOutput

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PARSER_SOURCE = _REPOSITORY_ROOT / "src" / "wiki_parser"
_FORBIDDEN_IMPORT_ROOTS = {
    "pi_agent_core_py",
    "docling",
    "docling_core",
    "fitz",
    "marker",
    "pymupdf",
    "pymupdf4llm",
    "surya",
    "torch",
    "transformers",
    "fastapi",
}


def test_standalone_package_has_no_main_app_or_concrete_runtime_imports() -> None:
    for source_path in _PARSER_SOURCE.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = [node.module]
            else:
                continue
            for name in imported:
                root = name.split(".", maxsplit=1)[0]
                assert root not in _FORBIDDEN_IMPORT_ROOTS, (
                    f"standalone parser imports {root} in {source_path}"
                )


def test_standalone_import_does_not_load_main_app_or_concrete_runtime() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPOSITORY_ROOT / "src")
    forbidden = sorted(_FORBIDDEN_IMPORT_ROOTS)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import wiki_parser; "
                f"forbidden={forbidden!r}; "
                "assert not any(name in sys.modules for name in forbidden)"
            ),
        ],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_main_distribution_packages_contract_but_not_parser_runtime_dependency() -> None:
    config = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    dependencies = [
        *config["project"]["dependencies"],
        *(
            item
            for values in config["project"]["optional-dependencies"].values()
            for item in values
        ),
    ]
    lowered = "\n".join(dependencies).lower()

    assert "src/wiki_parser" in packages
    for forbidden in (
        "docling",
        "marker-pdf",
        "pymupdf",
        "pymupdf4llm",
        "surya-ocr",
        "torch",
        "transformers",
    ):
        assert forbidden not in lowered


def test_fake_output_repr_does_not_echo_document_or_image_content() -> None:
    output = FakeParserOutput(
        markdown="PRIVATE-DOCUMENT-TEXT",
        images=(
            FakeParserImage(
                mime_type="image/png",
                content=b"PRIVATE-IMAGE-BYTES",
            ),
        ),
    )

    rendered = repr(output)
    assert "PRIVATE-DOCUMENT-TEXT" not in rendered
    assert "PRIVATE-IMAGE-BYTES" not in rendered
