from pathlib import Path

from shapely.geometry import Point, Polygon

from src.entity_classifier import classify_candidates
from src.recognition_contracts import CandidateSnapshot, RecognitionFeedback
from src.recognition_model import (
    build_training_dataset,
    compare_recognition_models,
    detect_feature_drift,
    evaluate_model_promotion,
    feedback_to_samples,
    load_recognition_model,
    predict_panel_probability,
    save_recognition_model,
    train_recognition_model,
)


def _feedback(index: int, tenant_id: str = "default") -> RecognitionFeedback:
    panel = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    hole = Point(50, 50).buffer(5, quad_segs=16)
    panel_snapshot = CandidateSnapshot(
        target_id=f"P-{index}",
        kind="panel",
        layer="RANDOM",
        area=panel.area,
        bounds=list(panel.bounds),
        geometry_wkt=panel.wkt,
        source="panel",
        entity_type="LWPOLYLINE",
    )
    hole_snapshot = CandidateSnapshot(
        target_id=f"H-{index}",
        kind="hole",
        layer="RANDOM",
        area=hole.area,
        bounds=list(hole.bounds),
        geometry_wkt=hole.wkt,
        source="hole",
        entity_type="CIRCLE",
    )
    return RecognitionFeedback(
        feedback_id=f"feedback-{index}",
        tenant_id=tenant_id,
        project_id=f"project-{index}",
        drawing_digest=f"drawing-{index}",
        revision=1,
        instruction="test",
        scope="global",
        after_panels=[panel_snapshot],
        after_holes=[hole_snapshot],
    )


def test_feedback_dataset_and_model_training(tmp_path):
    feedbacks = [_feedback(index) for index in range(6)]
    paths = []
    for index, feedback in enumerate(feedbacks):
        path = tmp_path / f"feedback-{index}.json"
        path.write_text(feedback.to_json(), encoding="utf-8")
        paths.append(path)

    samples = build_training_dataset(paths)
    model = train_recognition_model(samples, min_samples=4, epochs=300)

    assert len(samples) == 12
    assert model["drawing_count"] == 6
    assert model["metrics"]["positive_samples"] > 0
    assert model["metrics"]["negative_samples"] > 0

    panel = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    hole = Point(50, 50).buffer(5, quad_segs=16)
    panel_probability = predict_panel_probability(model, panel, "LWPOLYLINE")
    hole_probability = predict_panel_probability(model, hole, "CIRCLE")
    assert panel_probability is not None
    assert hole_probability is not None
    assert panel_probability > hole_probability

    saved = tmp_path / "models" / "current.json"
    save_recognition_model(saved, model)
    assert load_recognition_model(saved)["model_version"] == model["model_version"]


def test_classifier_uses_global_model_as_weak_signal():
    candidate = {
        "handle": "H1",
        "layer": "0",
        "linetype": "CONTINUOUS",
        "entity_type": "LWPOLYLINE",
        "polygon": Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
    }
    model = {
        "feature_names": [
            "log_area",
            "aspect_ratio",
            "vertex_count",
            "compactness",
            "is_circle",
        ],
        "mean": [0.0, 0.0, 0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0, 1.0, 1.0],
        "weights": [2.0, 0.0, 0.0, 0.0, 0.0],
        "bias": 1.0,
        "classifier_weight": 0.2,
    }
    classified = classify_candidates([candidate], recognition_model=model)

    assert classified[0]["classification"]["role"] == "panel"
    assert any(
        reason.startswith("model:panel_probability=")
        for reason in classified[0]["classification"]["reasons"]
    )


def test_feedback_to_samples_marks_corrected_panels_and_holes():
    samples = feedback_to_samples(_feedback(1))

    assert [sample["label"] for sample in samples] == [1, 0]
    assert samples[0]["drawing_digest"] == "drawing-1"
def test_model_promotion_gate_blocks_low_quality_models():
    rejected = evaluate_model_promotion(
        {
            "sample_count": 4,
            "drawing_count": 1,
            "metrics": {"accuracy": 0.5, "precision": 0.4, "recall": 0.3},
        }
    )
    approved = evaluate_model_promotion(
        {
            "sample_count": 30,
            "drawing_count": 8,
            "metrics": {"accuracy": 0.9, "precision": 0.85, "recall": 0.8},
        }
    )

    assert rejected["approved"] is False
    assert approved["approved"] is True
def test_candidate_comparison_and_feature_drift():
    samples = feedback_to_samples(_feedback(1))
    samples += feedback_to_samples(_feedback(2))
    current = {
        "feature_names": ["x"],
        "mean": [0.0, 0.0, 0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0, 1.0, 1.0],
        "weights": [2.0, 0.0, 0.0, 0.0, 0.0],
        "bias": -15.0,
    }
    worse = {
        **current,
        "bias": 10.0,
    }

    comparison = compare_recognition_models(current, worse, samples)
    drift = detect_feature_drift(
        {
            **current,
            "mean": [100.0, 0.0, 0.0, 0.0, 0.0],
        },
        samples,
    )

    assert comparison["approved"] is False
    assert drift["drift_detected"] is True
    assert "log_area" in drift["drifted_features"]
def test_training_dataset_can_filter_by_tenant(tmp_path):
    first = _feedback(1, tenant_id="tenant-a")
    second = _feedback(2, tenant_id="tenant-b")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(first.to_json(), encoding="utf-8")
    second_path.write_text(second.to_json(), encoding="utf-8")

    samples = build_training_dataset(
        [first_path, second_path],
        tenant_id="tenant-a",
    )

    assert len(samples) == 2
    assert {sample["project_id"] for sample in samples} == {"project-1"}
