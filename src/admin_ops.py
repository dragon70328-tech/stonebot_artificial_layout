"""Administrative operations for recognition models and learning data."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.recognition_model import load_recognition_model, save_recognition_model
from src.recognition_operations import run_recognition_operations
from src.recognition_retention import plan_feedback_retention


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):
        return {}
    return payload if isinstance(payload,dict) else {}


def recognition_admin_status(root: str | Path) -> dict[str,Any]:
    root=Path(root)
    models_dir=root/'recognition_models'
    current=load_recognition_model(models_dir/'current.json')
    versions=[]
    for path in sorted((models_dir/'versions').glob('*.json'),reverse=True):
        model=_load_json(path)
        versions.append({
            'file':path.name,
            'model_version':model.get('model_version') or path.stem,
            'created_at':model.get('created_at'),
            'metrics':model.get('metrics') or {},
            'sample_count':model.get('sample_count',0),
            'drawing_count':model.get('drawing_count',0),
            'active':model.get('model_version')==current.get('model_version'),
        })
    feedback_root=models_dir/'global_feedback'
    tenant_counts={}
    if feedback_root.exists():
        for tenant_dir in sorted(path for path in feedback_root.iterdir() if path.is_dir()):
            if tenant_dir.name=='archive':
                continue
            tenant_counts[tenant_dir.name]=sum(1 for _ in tenant_dir.rglob('*.json'))
    retention=plan_feedback_retention(feedback_root)
    return {
        'current_model':{
            'model_version':current.get('model_version'),
            'created_at':current.get('created_at'),
            'metrics':current.get('metrics') or {},
            'sample_count':current.get('sample_count',0),
            'drawing_count':current.get('drawing_count',0),
        },
        'versions':versions[:50],
        'training_state':_load_json(models_dir/'training_state.json'),
        'latest_training':_load_json(models_dir/'latest_training.json'),
        'monitoring':_load_json(models_dir/'monitoring'/'latest.json'),
        'operations':_load_json(models_dir/'operations'/'latest.json'),
        'feedback':{'tenant_counts':tenant_counts,'total':sum(tenant_counts.values())},
        'retention':{'planned':len(retention.get('archive',[]))},
    }


def run_admin_operations(
    root: str | Path,
    *,
    tenant_id: str | None = None,
    dry_run: bool = False,
    activate: bool = True,
    apply_retention: bool = False,
    force: bool = False,
) -> dict[str,Any]:
    root=Path(root)
    return run_recognition_operations(
        feedback_dir=root/'recognition_models'/'global_feedback',
        output_dir=root/'recognition_models',
        tenant_id=tenant_id,
        activate=activate,
        force=force,
        apply_retention=apply_retention,
        dry_run=dry_run,
    )


def rollback_recognition_model(
    root: str | Path,
    version: str,
) -> dict[str,Any]:
    root=Path(root)
    models_dir=root/'recognition_models'
    versions_dir=(models_dir/'versions').resolve()
    candidate=Path(version)
    if candidate.suffix=='.json' and candidate.exists():
        source=candidate.resolve()
    else:
        source=(versions_dir/f'{version}.json').resolve()
        if not source.exists():
            source=(versions_dir/version).resolve()
    try:
        source.relative_to(versions_dir)
    except ValueError as exc:
        raise ValueError('rollback version must be inside the versions directory') from exc
    model=load_recognition_model(source)
    if not model or not model.get('weights'):
        raise ValueError(f'invalid recognition model version: {version}')
    current_path=models_dir/'current.json'
    backup_path=None
    if current_path.exists():
        backup_dir=models_dir/'backups'
        backup_dir.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        previous=load_recognition_model(current_path)
        previous_name=previous.get('model_version') or 'current'
        backup_path=backup_dir/f'{previous_name}_{stamp}.json'
        shutil.copy2(current_path,backup_path)
    save_recognition_model(current_path,model)
    return {
        'model_version':model.get('model_version'),
        'source':str(source),
        'backup':str(backup_path) if backup_path else None,
    }
