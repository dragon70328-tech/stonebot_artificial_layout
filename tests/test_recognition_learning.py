from pathlib import Path

from shapely.geometry import Polygon

from src.active_learning import build_active_learning_queue
from src.correction_contracts import CorrectionPlan
from src.drawing_corrections import apply_correction_plan
from src.entity_classifier import classify_candidates
from src.job_context import JobContext
from src.models import Part
from src.recognition_contracts import FeedbackScope
from src.recognition_feedback import record_confirmation_feedback
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
def test_active_learning_prioritizes_unknown_and_conflicting_candidates(tmp_path):
    context = _panel_context(tmp_path)
    context.parts_data[0]["classification"] = {
        "role": "panel",
        "confidence": 0.4,
        "panel_score": 0.42,
        "hole_score": 0.40,
        "reasons": [
            "model:panel_probability=0.2",
            "layer:unknown_hint",
        ],
        "features": {"has_number_text": False},
    }
    context.classified_candidates = [
        {
            "handle": "H1",
            "layer": "0",
            "polygon": context.parts[0].outer_polygon,
            "classification": context.parts_data[0]["classification"],
        },
        {
            "handle": "H9",
            "layer": "0",
            "polygon": Polygon([(200, 0), (300, 0), (300, 100), (200, 100)]),
            "classification": {
                "role": "unknown",
                "confidence": 0.3,
                "panel_score": 0.3,
                "hole_score": 0.35,
                "reasons": ["layer:unknown_hint"],
                "features": {"has_number_text": False},
            },
        },
    ]

    queue = build_active_learning_queue(context)

    assert queue[0]["target_id"] == "handle:H9"
    assert any(item["target_id"] == "1" for item in queue)


def test_confirmation_records_positive_feedback_and_updates_memory(tmp_path):
    context = _panel_context(tmp_path)

    feedback, path = record_confirmation_feedback(context)

    assert path.exists()
    assert feedback.instruction == "用户确认原位DXF识别结果"
    assert len(feedback.after_panels) == 1
    assert context.recognition_memory["feedback_count"] == 1
    hints = recognition_memory_hints(context.recognition_memory)
    assert "ORIGINAL_PANEL" in hints["panel_layers"]
def test_global_confirmation_does_not_update_project_memory(tmp_path):
    context = _panel_context(tmp_path)

    feedback, path = record_confirmation_feedback(
        context,
        scope=FeedbackScope.GLOBAL,
    )

    assert feedback.scope == "global"
    assert (tmp_path / "global_feedback" / path.name).exists()
    assert not context.recognition_memory_path.exists()
