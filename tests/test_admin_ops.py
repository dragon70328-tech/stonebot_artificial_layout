from pathlib import Path

from src.admin_ops import recognition_admin_status, rollback_recognition_model
from src.recognition_model import save_recognition_model


def _model(version:str,bias:float)->dict:
    return {
        'model_version':version,
        'created_at':'2026-01-01T00:00:00+00:00',
        'feature_names':['a','b','c','d','e'],
        'mean':[0,0,0,0,0],
        'scale':[1,1,1,1,1],
        'weights':[1,0,0,0,0],
        'bias':bias,
        'metrics':{'accuracy':0.9,'precision':0.9,'recall':0.9},
        'sample_count':20,
        'drawing_count':5,
    }


def test_admin_status_and_rollback(tmp_path:Path):
    models=tmp_path/'recognition_models'
    versions=models/'versions'
    versions.mkdir(parents=True)
    first=_model('model-a',-1.0)
    second=_model('model-b',-2.0)
    save_recognition_model(versions/'model-a.json',first)
    save_recognition_model(versions/'model-b.json',second)
    save_recognition_model(models/'current.json',second)

    status=recognition_admin_status(tmp_path)
    result=rollback_recognition_model(tmp_path,'model-a')
    restored=recognition_admin_status(tmp_path)

    assert status['current_model']['model_version']=='model-b'
    assert len(status['versions'])==2
    assert result['model_version']=='model-a'
    assert result['backup']
    assert restored['current_model']['model_version']=='model-a'
