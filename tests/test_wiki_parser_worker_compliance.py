"""MinerU Worker package, source archive, SBOM, and isolation gates."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import shutil
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
_FORBIDDEN_WORKER_IMPORTS = {"pi_agent_core_py", "mineru", "pypdf", "torch"}


def _test_app(source_root: Path) -> object:
    harness = AgentHarness(
        Agent(system_prompt="", client=FakeClient([[DoneEvent(stop_reason="stop")]]))
    )
    harness.attach_skills([])
    return create_app(harness, wiki_parser_worker_source_root=source_root)


def test_worker_metadata_declares_mineru_and_unverified_runtime_honestly() -> None:
    manifest = json.loads(
        (_WORKER_ROOT / "component-manifest.json").read_text(encoding="utf-8")
    )
    runtime = json.loads(
        (_WORKER_ROOT / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads((_WORKER_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sbom = json.loads((_WORKER_ROOT / "sbom.spdx.json").read_text(encoding="utf-8"))

    assert manifest["component_id"] == "wiki-parser-worker"
    assert manifest["license_expression"] == "MIT"
    assert manifest["runtime_ready"] is False
    assert project["project"]["version"] == manifest["version"]
    assert project["project"]["optional-dependencies"]["runtime"] == [
        "mineru[core]==3.4.5"
    ]
    assert runtime["gate"]["adapter_source_ready"] is True
    assert runtime["gate"]["full_transitive_lock_ready"] is True
    assert runtime["gate"]["network_during_job"] is False
    assert runtime["gate"]["offline_oci_image_verified"] is False
    assert runtime["gate"]["gpu_profiles_verified"] is False
    assert runtime["gate"]["runtime_ready"] is False
    assert runtime["packages"][0]["name"] == "mineru"
    assert runtime["packages"][0]["version"] == "3.4.5"
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert {package["name"] for package in sbom["packages"]} == {
        "pi-wiki-parser-worker",
        "mineru",
    }
    assert (_WORKER_ROOT / "LICENSE").read_text(encoding="utf-8").startswith(
        "MIT License"
    )
    assert "MinerU Open Source License" in (
        _WORKER_ROOT / "MINERU_LICENSE.md"
    ).read_text(encoding="utf-8")

    config = json.loads(
        (_WORKER_ROOT / runtime["routing_config"]["path"]).read_text(encoding="utf-8")
    )
    canonical = json.dumps(
        config,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == runtime["routing_config"]["sha256"]


def test_oci_build_has_cpu_and_gpu_entrypoints_and_offline_runtime() -> None:
    dockerfile = (_WORKER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    cpu_compose = (_WORKER_ROOT / "compose.yaml").read_text(encoding="utf-8")
    gpu_compose = (_WORKER_ROOT / "compose.gpu.yaml").read_text(encoding="utf-8")
    lock = (_WORKER_ROOT / "requirements-linux-x86_64.lock").read_text(encoding="utf-8")

    assert "mineru==3.4.5" in lock
    assert "--require-hashes" in dockerfile
    assert "mineru.cli.models_download" in dockerfile
    assert "MINERU_MODEL_SOURCE=local" in dockerfile
    assert "network_mode: none" in cpu_compose
    assert "read_only: true" in cpu_compose
    assert 'user: "65532:65532"' in cpu_compose
    assert "no-new-privileges:true" in cpu_compose
    assert "ports:" not in cpu_compose
    assert "gpus: all" in gpu_compose


def test_worker_import_boundary_keeps_heavy_runtime_lazy() -> None:
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
                name.split(".", maxsplit=1)[0] not in _FORBIDDEN_WORKER_IMPORTS
                for name in imports
            )


def test_main_distribution_does_not_package_mineru_runtime() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    dependencies = [
        *project["project"]["dependencies"],
        *(
            dependency
            for group in project["project"]["optional-dependencies"].values()
            for dependency in group
        ),
    ]
    dependency_text = "\n".join(dependencies).casefold()

    assert "workers/wiki_parser_worker" not in packages
    assert "src/wiki_parser_worker" not in packages
    for forbidden in ("mineru", "torch", "transformers"):
        assert forbidden not in dependency_text


def test_source_offer_archive_is_exact_and_deterministic() -> None:
    service = SourceOfferService(_WORKER_ROOT)
    first_snapshot, first_archive, first_hash = service.build_archive()
    second_snapshot, second_archive, second_hash = service.build_archive()

    assert first_snapshot == second_snapshot
    assert first_archive == second_archive
    assert first_hash == second_hash == hashlib.sha256(first_archive).hexdigest()
    assert first_snapshot.runtime_ready is False
    expected = {item.path: item for item in first_snapshot.files}
    with tarfile.open(fileobj=io.BytesIO(first_archive), mode="r:gz") as archive:
        members = archive.getmembers()
        assert len(members) == len(expected)
        for member in members:
            assert member.isfile()
            assert member.mtime == 0
            assert member.uid == member.gid == 0
            extracted = archive.extractfile(member)
            assert extracted is not None
            relative = member.name.split("/", maxsplit=1)[1]
            assert hashlib.sha256(extracted.read()).hexdigest() == expected[relative].sha256


def test_source_offer_rejects_undeclared_tree_entry(tmp_path: Path) -> None:
    copied = tmp_path / "worker"
    shutil.copytree(_WORKER_ROOT, copied)
    (copied / "undeclared.py").write_text("print('undeclared')\n", encoding="utf-8")

    with pytest.raises(SourceOfferError, match="exactly describe"):
        SourceOfferService(copied)


def test_source_offer_ignores_local_test_artifacts(tmp_path: Path) -> None:
    copied = tmp_path / "worker"
    shutil.copytree(_WORKER_ROOT, copied)
    generated = copied / ".pytest-tmp" / "case"
    generated.mkdir(parents=True)
    (generated / "source.pdf").write_bytes(b"temporary test input")

    assert SourceOfferService(copied).snapshot().component_id == "wiki-parser-worker"


def test_about_api_exposes_mineru_worker_assets() -> None:
    with TestClient(_test_app(_WORKER_ROOT)) as client:
        about = client.get("/api/about/licenses")
        assert about.status_code == 200
        worker = about.json()["components"][0]
        assert worker["license_expression"] == "MIT"
        assert worker["runtime_ready"] is False
        assert worker["source_offer_available"] is True

        notice = client.get(worker["notices_url"])
        license_response = client.get(worker["license_url"])
        sbom = client.get(worker["sbom_url"])
        assert "MinerU" in notice.text
        assert license_response.text.startswith("MIT License")
        assert sbom.json()["spdxVersion"] == "SPDX-2.3"


def test_explicit_invalid_source_offer_root_fails_app_creation(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Corresponding Source configuration is invalid"):
        _test_app(tmp_path / "missing")
