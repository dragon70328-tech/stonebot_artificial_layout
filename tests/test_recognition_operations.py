from pathlib import Path

from shapely.geometry import Point, Polygon

from src.recognition_contracts import CandidateSnapshot, RecognitionFeedback
from src.recognition_model import polygon_feature_vector
from src.recognition_operations import run_recognition_operations


def _feedback(index:int)->RecognitionFeedback:
    panel=Polygon([(0,0),(100,0),(100,100),(0,100)])
    hole=Point(50,50).buffer(5,quad_segs=16)
    return RecognitionFeedback(
        feedback_id=f'feedback-{index}',
        tenant_id='tenant-a',
        project_id=f'project-{index}',
        drawing_digest=f'drawing-{index}',
        revision=1,
        instruction='global_learning_sample',
        scope='global',
        after_panels=[CandidateSnapshot(
            target_id=f'p-{index}',kind='panel',area=panel.area,
            geometry_wkt=panel.wkt,features=polygon_feature_vector(panel,'LWPOLYLINE'),
            source='global_redacted',
        )],
        after_holes=[CandidateSnapshot(
            target_id=f'h-{index}',kind='hole',entity_type='CIRCLE',area=hole.area,
            geometry_wkt=hole.wkt,features=polygon_feature_vector(hole,'CIRCLE'),
            source='global_redacted',
        )],
    )


def test_integrated_operations_trains_and_activates_model(tmp_path:Path):
    feedback_dir=tmp_path/'feedback'
    tenant_dir=feedback_dir/'tenant-a'
    tenant_dir.mkdir(parents=True)
    for index in range(6):
        (tenant_dir/f'f-{index}.json').write_text(
            _feedback(index).to_json(),encoding='utf-8'
        )

    report=run_recognition_operations(
        feedback_dir=feedback_dir,
        output_dir=tmp_path/'models',
        tenant_id='tenant-a',
        min_new_feedback=2,
        min_total_feedback=2,
        min_drift_feedback=2,
        min_samples=2,
        epochs=200,
        activate=True,
    )

    assert report['training']['trained'] is True
    assert report['training']['activated'] is True
    assert (tmp_path/'models'/'current.json').exists()
    assert (tmp_path/'models'/'operations'/'latest.json').exists()
    assert report['sample_count']==12


def test_integrated_operations_dry_run_does_not_train(tmp_path:Path):
    feedback_dir=tmp_path/'feedback'/'tenant-a'
    feedback_dir.mkdir(parents=True)
    (feedback_dir/'f.json').write_text(_feedback(1).to_json(),encoding='utf-8')

    report=run_recognition_operations(
        feedback_dir=tmp_path/'feedback',
        output_dir=tmp_path/'models',
        tenant_id='tenant-a',
        min_new_feedback=1,
        min_total_feedback=1,
        min_samples=1,
        dry_run=True,
    )

    assert report['training']['trained'] is False
    assert (tmp_path/'models'/'current.json').exists() is False
