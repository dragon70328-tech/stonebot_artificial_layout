from __future__ import annotations

from pathlib import Path
import json
from xml.etree import ElementTree
from zipfile import ZipFile

from src.drawing_profile import analyze_drawing
from src.plugin_service import PluginJobService, StonebotProtocolError


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dxf"
    / "雪花白洗手台（小利）1.8.dxf"
)


def test_plugin_service_preserves_label_ownership_from_drawing(tmp_path):
    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "label-ownership",
        }
    )
    targets = service.list_correction_targets(
        {
            "job_id": analyzed["job_id"],
            "include_unrecognized": False,
        }
    )

    numbers_by_target = {
        item["target_id"].rsplit(":", 1)[-1]: item["number"]
        for item in targets["items"]
    }
    assert numbers_by_target == {
        "p0": "XST-4",
        "p1": "XST-1",
        "p2": "XST-2",
        "p3": "XST-3",
        "p4": "XST-5",
        "p5": "XST-6",
    }


def test_plugin_service_runs_drawing_to_final_artifacts(tmp_path):
    service = PluginJobService(tmp_path)

    analyzed = service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "plugin-smoke",
        }
    )

    assert analyzed["ok"] is True
    assert analyzed["stage"] == "audited"
    assert analyzed["confirmation_required"] is True
    assert analyzed["nesting_configured"] is False
    assert analyzed["summary"]["counts"]["parts"] == 6
    assert analyzed["summary"]["counts"]["holes"] == 4
    assert analyzed["artifacts"] == {}
    job_id = analyzed["job_id"]

    configured = service.configure_nesting(
        {
            "job_id": job_id,
            "width_mm": 3200,
            "height_mm": 1600,
            "thickness_mm": 20,
            "process": "bridge",
            "kerf_mm": 5,
            "profile": "min_sheets",
            "quick": True,
        }
    )
    assert configured["nesting_configured"] is True
    assert configured["summary"]["process"] == "bridge"

    nested = service.advance_until_gate(
        {
            "job_id": job_id,
            "gate": "drawing",
            "decision": "confirm",
        }
    )
    assert nested["stage"] == "nested_reported"
    assert nested["summary"]["nesting"]["total_parts"] == 6

    validated = service.advance_until_gate(
        {
            "job_id": job_id,
            "gate": "nesting",
            "decision": "confirm",
        }
    )
    assert validated["stage"] == "postprocess_confirmed"
    assert validated["validation_errors"] == []
    assert validated["artifacts"] == {}

    try:
        service.advance_until_gate(
            {
                "job_id": job_id,
                "gate": "final",
                "decision": "confirm",
            }
        )
    except StonebotProtocolError as exc:
        assert exc.code == "OUTPUT_DIRECTORY_REQUIRED"
    else:
        raise AssertionError("finalization must require a save folder")

    delivery_dir = tmp_path / "delivery"
    delivery_dir.mkdir()
    completed = service.advance_until_gate(
        {
            "job_id": job_id,
            "gate": "final",
            "decision": "confirm",
            "output_directory": str(delivery_dir),
        }
    )
    assert completed["stage"] == "completed"
    assert completed["completed"] is True
    assert Path(completed["artifacts"]["nested_dxf"]).exists()
    assert Path(completed["artifacts"]["numbered_dxf"]).exists()
    assert "report" not in completed["artifacts"]
    assert sorted(
        path.suffix.casefold()
        for path in delivery_dir.iterdir()
    ) == [".dxf", ".dxf"]

    word_dir = tmp_path / "word"
    word_dir.mkdir()
    reported = service.export_word_report(
        {
            "job_id": job_id,
            "output_directory": str(word_dir),
        }
    )
    word_path = Path(reported["word_report"])
    assert word_path.exists()
    with ZipFile(word_path) as archive:
        names = set(archive.namelist())
        for name in names:
            if name.endswith(".xml"):
                ElementTree.fromstring(archive.read(name))
    assert "word/document.xml" in names
    assert "word/styles.xml" in names

    listed = service.list_jobs({})
    assert listed["total"] == 1
    assert listed["jobs"][0]["job_id"] == job_id
    assert listed["jobs"][0]["stage"] == "completed"


def test_choose_output_directory_returns_native_picker_result(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    captured = []

    def picker(title):
        captured.append(title)
        return str(selected)

    service = PluginJobService(tmp_path, directory_picker=picker)
    result = service.choose_output_directory({"purpose": "delivery"})

    assert result["ok"] is True
    assert result["canceled"] is False
    assert result["output_directory"] == str(selected)
    assert "保存文件夹" in captured[0]


def test_plugin_service_requires_nesting_configuration_before_drawing_gate(
    tmp_path,
):
    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing({"dxf_path": str(FIXTURE)})

    try:
        service.confirm_drawing(
            {
                "job_id": analyzed["job_id"],
                "decision": "confirm",
            }
        )
    except StonebotProtocolError as exc:
        assert exc.code == "NESTING_NOT_CONFIGURED"
    else:
        raise AssertionError("confirmation should require nesting parameters")


def test_plugin_service_rejects_bridge_without_kerf(tmp_path):
    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing({"dxf_path": str(FIXTURE)})

    try:
        service.configure_nesting(
            {
                "job_id": analyzed["job_id"],
                "width_mm": 3200,
                "height_mm": 1600,
                "process": "bridge",
                "kerf_mm": 0,
            }
        )
    except StonebotProtocolError as exc:
        assert exc.code == "BRIDGE_KERF_REQUIRED"
    else:
        raise AssertionError("bridge configuration without kerf must fail")


def test_rename_panels_uses_stable_target_ids(tmp_path):
    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing({"dxf_path": str(FIXTURE)})
    targets = service.list_correction_targets(
        {
            "job_id": analyzed["job_id"],
            "limit": 10,
        }
    )
    first = targets["items"][0]

    corrected = service.rename_panels(
        {
            "job_id": analyzed["job_id"],
            "instruction": "将第一件改为 XST-99",
            "renames": [
                {
                    "target_id": first["target_id"],
                    "new_number": "XST-99",
                }
            ],
        }
    )
    refreshed = service.list_correction_targets(
        {"job_id": analyzed["job_id"], "limit": 10}
    )

    assert corrected["summary"]["correction_revision"] == 1
    assert any(item.get("number") == "XST-99" for item in refreshed["items"])


def test_plugin_status_remains_readable_after_service_restart(tmp_path):
    first = PluginJobService(tmp_path)
    analyzed = first.analyze_drawing({"dxf_path": str(FIXTURE)})

    second = PluginJobService(tmp_path)
    status = second.job_status({"job_id": analyzed["job_id"]})

    assert status["stage"] == "audited"
    assert status["context_available"] is True
    assert status["summary"]["counts"]["parts"] == 6


def test_plugin_resumes_all_gates_after_process_restart(tmp_path):
    first = PluginJobService(tmp_path)
    analyzed = first.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "restart-resume",
        }
    )
    job_id = analyzed["job_id"]
    first.configure_nesting(
        {
            "job_id": job_id,
            "width_mm": 3200,
            "height_mm": 1600,
            "thickness_mm": 20,
            "process": "bridge",
            "kerf_mm": 5,
            "profile": "min_sheets",
            "quick": True,
        }
    )

    second = PluginJobService(tmp_path)
    nested = second.confirm_drawing(
        {"job_id": job_id, "decision": "confirm"}
    )
    assert nested["stage"] == "nested_reported"
    assert nested["summary"]["nesting"]["total_parts"] == 6

    third = PluginJobService(tmp_path)
    validated = third.confirm_nesting(
        {"job_id": job_id, "decision": "confirm"}
    )
    assert validated["stage"] == "postprocess_confirmed"
    assert validated["validation_errors"] == []

    fourth = PluginJobService(tmp_path)
    delivery_dir = tmp_path / "restart-delivery"
    delivery_dir.mkdir()
    completed = fourth.finalize_job(
        {
            "job_id": job_id,
            "decision": "confirm",
            "output_directory": str(delivery_dir),
        }
    )
    assert completed["stage"] == "completed"
    assert Path(completed["artifacts"]["nested_dxf"]).exists()
    assert "report" not in completed["artifacts"]


def test_plugin_uses_project_profile_override_for_new_jobs(tmp_path):
    profile = json.loads(
        (
            Path(__file__).parents[1]
            / "drawing_profiles"
            / "snow_white_basin.json"
        ).read_text(encoding="utf-8")
    )
    fingerprint = analyze_drawing(FIXTURE)
    fingerprint.pop("path", None)
    override_dir = (
        tmp_path
        / "tenants"
        / "default"
        / "adaptive-project"
        / "profile_overrides"
    )
    override_dir.mkdir(parents=True)
    (override_dir / "profilepatch-active.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "base_profile_name": profile["name"],
                "patch_id": "profilepatch-active",
                "profile": profile,
                "fingerprint": fingerprint,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "adaptive-project",
        }
    )

    assert (
        analyzed["summary"]["drawing_profile_source"]
        == "project_override:profilepatch-active"
    )


def test_plugin_service_accepts_profile_patch_and_reuses_override(tmp_path):
    service = PluginJobService(tmp_path)
    analyzed = service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "adaptive-service",
        }
    )
    base = json.loads(
        (
            Path(__file__).parents[1]
            / "drawing_profiles"
            / "snow_white_basin.json"
        ).read_text(encoding="utf-8")
    )
    patch_id = "profilepatch-service"
    patch_path = (
        tmp_path
        / "profile_learning"
        / "default"
        / "adaptive-service"
        / "patches"
        / f"{patch_id}.json"
    )
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "patch_id": patch_id,
                "status": "candidate",
                "tenant_id": "default",
                "project_id": "adaptive-service",
                "base_profile_name": "snow_white_basin",
                "changes": [
                    {
                        "target": "small_area_threshold",
                        "before": base.get("small_area_threshold", 0),
                        "after": 0.1,
                        "evidence_count": 6,
                    }
                ],
                "evidence": {
                    "job_id": analyzed["job_id"],
                    "target_counts": {
                        "panels": 6,
                        "holes": 4,
                        "numbers": 6,
                    },
                },
                "validation": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    accepted = service.accept_profile_patch({"patch_id": patch_id})
    reused = service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "adaptive-service",
        }
    )

    assert accepted["patch"]["status"] == "active"
    assert (
        reused["summary"]["drawing_profile_source"]
        == f"project_override:{patch_id}"
    )
