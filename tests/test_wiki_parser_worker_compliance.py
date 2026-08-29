"""AGPL Worker package, Corresponding Source, SBOM, and isolation gates."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.agent import Agent  # noqa: E402
from pi_agent_core_py.harness import AgentHarness  # noqa: E402
from pi_agent_core_py.model_client import DoneEvent, FakeClient  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.source_offer import (  # noqa: E402
    SourceOfferError,
    SourceOfferService,
)

_ROOT = Path(__file__).resolve().parents[1]
_WORKER_ROOT = _ROOT / "workers" / "wiki_parser_worker"
_WORKER_SOURCE = _WORKER_ROOT / "src" / "wiki_parser_worker"
_OFFICIAL_AGPL_TEXT_SHA256 = (
    "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
)
_FORBIDDEN_RUNTIME_ROOTS = {
    "docling",
    "docling_core",
    "fitz",
    "marker",
    "pi_agent_core_py",
    "pymupdf",
    "pymupdf4llm",
    "surya",
    "torch",
    "transformers",
}


def _test_app(source_root: Path) -> object:
    harness = AgentHarness(
        Agent(system_prompt="", client=FakeClient([[DoneEvent(stop_reason="stop")]]))
    )
    harness.attach_skills([])
    return create_app(harness, wiki_parser_worker_source_root=source_root)


def test_worker_package_has_exact_agpl_identity_and_complete_gate_assets() -> None:
    manifest = json.loads(
        (_WORKER_ROOT / "component-manifest.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads((_WORKER_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sbom = json.loads((_WORKER_ROOT / "sbom.spdx.json").read_text(encoding="utf-8"))
    runtime = json.loads(
        (_WORKER_ROOT / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    license_bytes = (_WORKER_ROOT / "LICENSE").read_bytes()

    assert manifest["component_id"] == "wiki-parser-worker"
    assert manifest["version"] == "0.0.29"
    assert manifest["license_expression"] == "AGPL-3.0-only"
    assert manifest["runtime_ready"] is True
    assert project["project"]["license"] == {"file": "LICENSE"}
    assert project["project"]["version"] == manifest["version"]
    assert project["project"]["dependencies"] == []
    assert runtime["gate"]["adapter_source_ready"] is True
    assert runtime["gate"]["full_transitive_lock_ready"] is True
    assert runtime["gate"]["runtime_ready"] is True
    assert runtime["gate"]["offline_oci_image_verified"] is True
    assert len(runtime["packages"]) == 13
    assert len(runtime["models"]) == 3
    runtime_dependencies = set(project["project"]["optional-dependencies"]["runtime"])
    assert "torch==2.13.0+cpu" in runtime_dependencies
    assert "torchvision==0.28.0+cpu" in runtime_dependencies
    assert project["tool"]["uv"]["sources"] == {
        "torch": {"index": "pytorch-cpu"},
        "torchvision": {"index": "pytorch-cpu"},
    }
    routing_config = json.loads(
        (_WORKER_ROOT / runtime["routing_config"]["path"]).read_text(encoding="utf-8")
    )
    routing_config_bytes = json.dumps(
        routing_config,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(routing_config_bytes).hexdigest() == runtime["routing_config"][
        "sha256"
    ]
    assert all(
        len(file["sha256"]) == 64 and file["size_bytes"] > 0
        for model in runtime["models"]
        for file in model["files"]
    )
    force_included = project["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    assert set(force_included) == {
        "LICENSE",
        "NOTICE.md",
        "SOURCE_OFFER.md",
        "component-manifest.json",
        "runtime-manifest.json",
        "sbom.spdx.json",
        "config/routing-quality-v1.json",
    }
    assert hashlib.sha256(license_bytes).hexdigest() == _OFFICIAL_AGPL_TEXT_SHA256
    decoded_license = license_bytes.decode("utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in decoded_license
    assert "13. Remote Network Interaction" in decoded_license
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["dataLicense"] == "CC0-1.0"
    assert sbom["packages"][0]["licenseDeclared"] == "AGPL-3.0-only"
    assert sbom["packages"][0]["versionInfo"] == manifest["version"]
    assert sbom["packages"][0]["filesAnalyzed"] is False


def test_oci_build_is_hash_locked_cpu_only_and_runtime_network_is_disabled() -> None:
    runtime = json.loads(
        (_WORKER_ROOT / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    build = runtime["oci_build"]
    dockerfile = (_WORKER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (_WORKER_ROOT / "compose.yaml").read_text(encoding="utf-8")
    uv_lock = (_WORKER_ROOT / build["uv_lock"]["path"]).read_bytes()
    requirements = (
        _WORKER_ROOT / build["requirements_lock"]["path"]
    ).read_bytes()
    requirements_text = requirements.decode("utf-8")

    assert build["base_image"] in dockerfile
    assert hashlib.sha256(uv_lock).hexdigest() == build["uv_lock"]["sha256"]
    assert hashlib.sha256(requirements).hexdigest() == build["requirements_lock"][
        "sha256"
    ]
    assert uv_lock.count(b"[[package]]") == build["uv_lock"]["package_count"]
    assert (
        len(re.findall(r"(?m)^[A-Za-z0-9][A-Za-z0-9_.-]*==", requirements_text))
        == build["requirements_lock"]["resolved_package_count"]
    )
    assert "--require-hashes" in dockerfile
    assert "--index-url https://pypi.org/simple" in requirements_text
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in requirements_text
    assert "network_mode: none" in compose
    assert "read_only: true" in compose
    assert 'user: "65532:65532"' in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "ports:" not in compose
    assert "opencv-python-headless" not in requirements_text
    assert "opencv-python==5.0.0.93" in requirements_text
    assert not re.search(r"(?mi)^(?:nvidia-|cuda)", requirements_text)
    assert runtime["gate"]["offline_source_bundle_materialized"] is True
    assert runtime["gate"]["offline_oci_image_verified"] is True
    assert runtime["gate"]["representative_pdf_smoke_passed"] is True
    assert runtime["gate"]["runtime_ready"] is True
    assert runtime["verification"]["verified_at"] == "2026-08-26"
    assert runtime["verification"]["docker_version"] == "29.7.2"
    agpl_sources = {
        package["name"]: package["source"]
        for package in runtime["packages"]
        if package.get("selected_license") == "AGPL-3.0-only"
    }
    assert set(agpl_sources) == {"pymupdf", "pymupdf-layout", "pymupdf4llm"}
    assert all(
        isinstance(source["archive_sha256"], str)
        and len(source["archive_sha256"]) == 64
        and source["url"].startswith("https://")
        for source in agpl_sources.values()
    )
    assert "fetch_runtime_sources.py" in dockerfile
    assert "/opt/upstream-sources" in dockerfile


def test_worker_source_imports_neither_main_app_nor_concrete_parser_runtime() -> None:
    for source_path in _WORKER_SOURCE.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports = [node.module]
            else:
                continue
            assert all(
                name.split(".", maxsplit=1)[0] not in _FORBIDDEN_RUNTIME_ROOTS
                for name in imports
            )

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_WORKER_ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import wiki_parser_worker as worker; "
                "identity=worker.compliance_identity(); "
                "assert identity.runtime_ready is True; "
                f"forbidden={sorted(_FORBIDDEN_RUNTIME_ROOTS)!r}; "
                "assert not any(name in sys.modules for name in forbidden)"
            ),
        ],
        cwd=_WORKER_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_main_distribution_does_not_package_or_depend_on_worker_runtime() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    all_dependencies = [
        *project["project"]["dependencies"],
        *(
            dependency
            for group in project["project"]["optional-dependencies"].values()
            for dependency in group
        ),
    ]
    dependency_text = "\n".join(all_dependencies).casefold()

    assert "workers/wiki_parser_worker" not in packages
    assert "src/wiki_parser_worker" not in packages
    for forbidden in ("docling", "pymupdf", "pymupdf4llm", "torch", "transformers"):
        assert forbidden not in dependency_text
    app_tree = ast.parse(
        (_ROOT / "src" / "pi_agent_core_py" / "web" / "app.py").read_text(encoding="utf-8")
    )
    imported_modules = {
        alias.name
        for node in ast.walk(app_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(app_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert not any(name.startswith("wiki_parser_worker") for name in imported_modules)


def test_source_offer_archive_is_exact_deterministic_and_self_verifying() -> None:
    service = SourceOfferService(_WORKER_ROOT)
    first_snapshot, first_archive, first_hash = service.build_archive()
    second_snapshot, second_archive, second_hash = service.build_archive()

    assert first_snapshot == second_snapshot
    assert first_archive == second_archive
    assert first_hash == second_hash == hashlib.sha256(first_archive).hexdigest()
    assert first_snapshot.runtime_ready is True
    assert len(first_snapshot.files) == 35
    public = first_snapshot.public_dict()
    assert str(_ROOT) not in json.dumps(public)
    assert public["source_tree_sha256"] == first_snapshot.source_tree_sha256

    expected = {item.path: item for item in first_snapshot.files}
    with tarfile.open(fileobj=io.BytesIO(first_archive), mode="r:gz") as archive:
        members = archive.getmembers()
        assert len(members) == len(expected)
        for member in members:
            assert member.isfile()
            assert member.mtime == 0
            assert member.uid == member.gid == 0
            assert member.mode == 0o644
            prefix = f"wiki-parser-worker-{first_snapshot.version}/"
            assert member.name.startswith(prefix)
            relative = member.name.removeprefix(prefix)
            item = expected[relative]
            extracted = archive.extractfile(member)
            assert extracted is not None
            content = extracted.read()
            assert len(content) == item.size
            assert hashlib.sha256(content).hexdigest() == item.sha256


def test_source_offer_rejects_undeclared_or_unsafe_tree_entries(tmp_path: Path) -> None:
    copied = tmp_path / "worker"
    shutil.copytree(_WORKER_ROOT, copied)
    (copied / "undeclared.py").write_text("print('must not be omitted')\n", encoding="utf-8")
    with pytest.raises(SourceOfferError, match="exactly describe"):
        SourceOfferService(copied)

    (copied / "undeclared.py").unlink()
    manifest_path = copied / "component-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": "../outside.py", "role": "source"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SourceOfferError, match="traversal"):
        SourceOfferService(copied)


def test_about_api_exposes_manifest_assets_and_deterministic_source_archive() -> None:
    with TestClient(_test_app(_WORKER_ROOT)) as client:
        about = client.get("/api/about/licenses")
        assert about.status_code == 200
        payload = about.json()
        assert payload["application"]["license_expression"] == "MIT"
        worker = payload["components"][0]
        assert worker["license_expression"] == "AGPL-3.0-only"
        assert worker["runtime_ready"] is True
        assert worker["source_offer_available"] is True
        assert str(_ROOT) not in about.text

        source_offer = client.get(worker["source_offer_url"])
        assert source_offer.status_code == 200
        assert source_offer.json()["file_count"] == 35
        assert source_offer.json()["source_tree_sha256"] == worker["source_tree_sha256"]

        license_response = client.get(worker["license_url"])
        notices_response = client.get(worker["notices_url"])
        sbom_response = client.get(worker["sbom_url"])
        assert hashlib.sha256(license_response.content).hexdigest() == _OFFICIAL_AGPL_TEXT_SHA256
        assert "Third-party notices" in notices_response.text
        assert sbom_response.json()["spdxVersion"] == "SPDX-2.3"

        archive = client.get(worker["source_archive_url"])
        repeated = client.get(worker["source_archive_url"])
        assert archive.status_code == 200
        assert archive.content == repeated.content
        assert archive.headers["x-archive-sha256"] == hashlib.sha256(
            archive.content
        ).hexdigest()
        assert archive.headers["x-source-tree-sha256"] == worker["source_tree_sha256"]
        assert "wiki-parser-worker-0.0.29-source.tar.gz" in archive.headers[
            "content-disposition"
        ]


def test_explicit_invalid_source_offer_root_fails_app_creation(tmp_path: Path) -> None:
    with pytest.raises(
        RuntimeError,
        match="Corresponding Source configuration is invalid",
    ):
        _test_app(tmp_path / "missing")
