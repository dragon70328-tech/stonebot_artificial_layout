from pathlib import Path

from shapely.geometry import Polygon

from src.correction_contracts import CorrectionPlan
from src.drawing_corrections import apply_correction_plan
from src.entity_classifier import classify_candidates
from src.job_context import JobContext
from src.models import Part
from src.recognition_contracts import FeedbackScope
from src.recognition_memory import (
    load_recognition_memory,
    new_recognition_memory,
    recognition_memory_hints,
)
from src.units import UnitSystem


def _panel_context(tmp_path: Path) -> JobContext:
    source = tmp_path / "drawing.dxf"
    source.write_bytes(b"0\nEOF\n")
    outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    part = Part(
        id=1,
        number="P-1",
        polygon=outer,
        outer_polygon=outer,
        original_number="P-1",
        outer_handle="H1",
    )
    return JobContext(
        dxf_path=str(source),
        project_id="project-1",
        job_id="job-1",
        unit=UnitSystem.METRIC,
        parts=[part],
        parts_data=[
            {
                "index": 1,
                "polygon": outer,
                "outer_polygon": outer,
                "holes": [],
                "hole_handles": [],
                "original_number": "P-1",
                "area": outer.area,
                "centroid": (50.0, 50.0),
                "outer_handle": "H1",
                "layer": "ORIGINAL_PANEL",
                "number_layer": "ORIGINAL_NUMBER",
            }
        ],
        output_dir=tmp_path,
        recognition_memory=new_recognition_memory("project-1"),
        recognition_memory_path=tmp_path / "recognition_memory.json",
        global_feedback_dir=tmp_path / "global_feedback",
    )


def test_project_feedback_updates_recognition_memory(tmp_path):
    context = _panel_context(tmp_path)
    context.closed_entities = [
        {
            "handle": "H9",
            "layer": "CUSTOM_PANEL_LAYER",
            "linetype": "CONTINUOUS",
            "entity_type": "LWPOLYLINE",
            "polygon": Polygon([(200, 0), (300, 0), (300, 100), (200, 100)]),
        }
    ]
    plan = CorrectionPlan(
        instruction="把漏识别轮廓新增为面板",
        operations=[
            {
                "operation": "add_panel_from_entity",
                "target": "handle:H9",
                "value": "P-2",
            }
        ],
    )

    result = apply_correction_plan(context, plan)

    assert result.feedback_artifact_path is not None
    assert Path(result.feedback_artifact_path).exists()
    memory = load_recognition_memory(context.recognition_memory_path)
    hints = recognition_memory_hints(memory)
    assert "CUSTOM_PANEL_LAYER" in hints["panel_layers"]
    assert memory["feedback_count"] == 1
    assert len(context.recognition_feedback) == 1


def test_global_feedback_is_queued_without_local_memory_update(tmp_path):
    context = _panel_context(tmp_path)
    plan = CorrectionPlan(
        instruction="修改编号",
        feedback_scope=FeedbackScope.GLOBAL,
        operations=[
            {"operation": "rename_number", "target": "P-1", "value": "P-1A"}
        ],
    )

    result = apply_correction_plan(context, plan)

    assert result.feedback_scope == "global"
    assert (tmp_path / "global_feedback" / Path(result.feedback_artifact_path).name).exists()
    assert not context.recognition_memory_path.exists()
    assert context.recognition_memory.get("feedback_count", 0) == 0


def test_classifier_uses_project_memory_as_weak_layer_hint():
    candidate = {
        "handle": "H1",
        "layer": "LEARNED_LAYER",
        "linetype": "CONTINUOUS",
        "entity_type": "LWPOLYLINE",
        "polygon": Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
    }
    memory = new_recognition_memory("project-1")
    memory["layer_roles"]["LEARNED_LAYER"] = {
        "panel_positive": 1,
        "panel_negative": 0,
        "hole_positive": 0,
        "hole_negative": 0,
    }

    classified = classify_candidates(
        [candidate],
        recognition_memory=memory,
    )

    assert classified[0]["classification"]["role"] == "panel"
    assert "layer:panel_hint" in classified[0]["classification"]["reasons"]
