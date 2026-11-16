"""End-to-end portable job transfer through PluginJobService."""

from __future__ import annotations

from pathlib import Path

from src.plugin_service import PluginJobService


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dxf"
    / "雪花白洗手台（小利）1.8.dxf"
)


def test_export_and_import_resumes_same_job_stage(tmp_path):
    source_service = PluginJobService(tmp_path / "source-runtime")
    analyzed = source_service.analyze_drawing(
        {
            "dxf_path": str(FIXTURE),
            "project_id": "portable-job",
        }
    )
    job_id = analyzed["job_id"]
    capsule = tmp_path / "portable-job.capsule"

    exported = source_service.export_task_capsule(
        {
            "job_id": job_id,
            "destination": str(capsule),
        }
    )

    assert capsule.is_file()
    assert exported["stage"] == "audited"
    assert exported["file_count"] >= 3
    assert exported["capsule_digest"]

    target_service = PluginJobService(tmp_path / "target-runtime")
    imported = target_service.import_task_capsule(
        {"capsule_path": str(capsule)}
    )

    assert imported["job_id"] == job_id
    assert imported["stage"] == "audited"
    assert imported["summary"]["counts"]["parts"] == 6
    assert imported["summary"]["counts"]["holes"] == 4
    runtime, manifest = target_service._runtime_and_manifest(job_id)
    assert runtime.session.session_id == job_id
    assert runtime.context.dxf_path == str(
        Path(manifest["dxf_path"]).resolve()
    )
    assert Path(manifest["dxf_path"]).is_file()
    assert Path(runtime.context.numbered_dxf_path).is_file()
    assert Path(manifest["output_dir"]).is_dir()

    restarted = PluginJobService(tmp_path / "target-runtime")
    status = restarted.job_status({"job_id": job_id})
    assert status["stage"] == "audited"
    assert status["summary"]["counts"]["parts"] == 6
