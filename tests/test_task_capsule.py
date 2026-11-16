"""Portable task capsule creation, verification and extraction tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.task_capsule import (
    TaskCapsuleError,
    create_task_capsule,
    extract_task_capsule,
    inspect_task_capsule,
    remap_json_paths,
)


def _payloads(tmp_path: Path) -> tuple[dict, dict, dict]:
    manifest = {
        "job_id": "stonebot-test",
        "tenant_id": "default",
        "project_id": "project-a",
        "stage": "nested_reported",
        "dxf_path": str(tmp_path / "source.dxf"),
        "output_dir": str(tmp_path / "run"),
    }
    context = {
        "snapshot_version": 1,
        "unit": "metric",
        "confirmed_drawing": {"confirmed_id": "cd-test"},
        "artifacts": {
            "numbered_dxf": str(tmp_path / "numbered.dxf"),
        },
    }
    return manifest, context, {"workflow_session": {"current_stage": "nested_reported"}}


def test_capsule_round_trip_preserves_binary_files_and_state(tmp_path):
    source = tmp_path / "source.dxf"
    source.write_bytes(b"DXF\x00\xff\x01binary")
    nested = tmp_path / "nested.dxf"
    nested.write_bytes(b"NESTED\x00payload")
    manifest, context, state = _payloads(tmp_path)
    capsule = tmp_path / "task.capsule"

    create_task_capsule(
        capsule,
        job_manifest=manifest,
        context_snapshot=context,
        files={"input_dxf": source, "nested_dxf": nested},
        state_payloads=state,
        runtime_metadata={"core_version": "test"},
    )

    info = inspect_task_capsule(capsule)
    assert info.job_id == "stonebot-test"
    assert info.stage == "nested_reported"
    assert set(info.files) == {
        "job_manifest",
        "context_snapshot",
        "state:workflow_session",
        "file:input_dxf",
        "file:nested_dxf",
    }

    imported = extract_task_capsule(capsule, tmp_path / "imported")
    assert imported.manifest_path.is_file()
    assert imported.files["nested_dxf"].read_bytes() == b"NESTED\x00payload"
    assert json.loads(imported.manifest_path.read_text(encoding="utf-8"))[
        "job_id"
    ] == "stonebot-test"
    assert json.loads(
        imported.state["workflow_session"].read_text(encoding="utf-8")
    ) == {"current_stage": "nested_reported"}


def test_capsule_detects_content_tampering(tmp_path):
    source = tmp_path / "source.dxf"
    source.write_bytes(b"original")
    manifest, context, _state = _payloads(tmp_path)
    capsule = tmp_path / "task.capsule"
    create_task_capsule(
        capsule,
        job_manifest=manifest,
        context_snapshot=context,
        files={"input_dxf": source},
    )
    tampered = tmp_path / "tampered.capsule"
    with zipfile.ZipFile(capsule, "r") as source_zip, zipfile.ZipFile(
        tampered,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as target_zip:
        input_name = next(
            name for name in source_zip.namelist() if "input_dxf" in name
        )
        for info in source_zip.infolist():
            data = source_zip.read(info.filename)
            if info.filename == input_name:
                data = b"changed!"
            target_zip.writestr(info.filename, data)

    with pytest.raises(TaskCapsuleError, match="hash mismatch"):
        inspect_task_capsule(tampered)


def test_capsule_rejects_path_traversal_entries(tmp_path):
    malicious = tmp_path / "malicious.capsule"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("capsule.json", "{}")
        archive.writestr("../escape.txt", "bad")

    with pytest.raises(TaskCapsuleError, match="unsafe capsule path"):
        inspect_task_capsule(malicious)


def test_extract_refuses_existing_destination(tmp_path):
    source = tmp_path / "source.dxf"
    source.write_bytes(b"dxf")
    manifest, context, _state = _payloads(tmp_path)
    capsule = tmp_path / "task.capsule"
    create_task_capsule(
        capsule,
        job_manifest=manifest,
        context_snapshot=context,
        files={"input_dxf": source},
    )
    destination = tmp_path / "already-there"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        extract_task_capsule(capsule, destination)


def test_remap_json_paths_replaces_exact_path_values():
    payload = {
        "dxf_path": "/old/source.dxf",
        "nested": ["/old/nested.dxf", {"other": "unchanged"}],
    }

    assert remap_json_paths(
        payload,
        {
            "/old/source.dxf": "/new/source.dxf",
            "/old/nested.dxf": "/new/nested.dxf",
        },
    ) == {
        "dxf_path": "/new/source.dxf",
        "nested": ["/new/nested.dxf", {"other": "unchanged"}],
    }
