"""Materialize ``ConfirmedDrawing`` snapshots at the numbering gate.

确认动作不再只是阶段推进：它产出一份内容寻址的 ConfirmedDrawing
（不可变快照），后续排板只允许消费这份快照。快照与现场零件不一致时
fail closed，要求重新确认，而不是带着漂移的内容继续排板。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.artifact_store import ArtifactStore
from src.contracts import (
    ActorKind,
    ConfirmedDrawing,
    RecognizedHole,
    RecognizedPanel,
)
from src.units import UNIT_LABELS

if TYPE_CHECKING:
    from src.job_context import JobContext
    from src.models import Part

# 内容摘要排除身份/落盘字段：confirmed_id 由摘要派生，artifact_path 是存储
# 位置，notes 携带父版本指针——都不属于"用户确认了几何内容"本身。
_DIGEST_EXCLUDE = {"confirmed_id", "confirmed_digest", "artifact_path", "notes"}


def _panel_payload(part: "Part") -> dict[str, Any]:
    """把一个 Part 转成规范化面板载荷（用于签名与快照）。"""
    holes = []
    hole_handles = part.hole_handles or []
    for index, hole in enumerate(part.holes):
        holes.append(
            {
                "hole_id": f"{part.number}:h{index}",
                "geometry_wkt": hole.wkt,
                "entity_handle": (
                    hole_handles[index] if index < len(hole_handles) else None
                ),
            }
        )
    return {
        "panel_id": f"part-{part.id}",
        "number": part.number,
        "geometry_wkt": part.polygon.wkt,
        "outer_geometry_wkt": part.outer_polygon.wkt,
        "entity_handle": part.outer_handle,
        "material_group": part.material_group,
        "holes": holes,
        "properties": {
            "area": float(part.area),
            "original_number": part.original_number,
            "material_column": part.material_column,
            "group_id": part.group_id,
            "closure_gap": float(part.closure_gap or 0.0),
            "label_position": [
                float(part.label_position[0]),
                float(part.label_position[1]),
            ],
        },
    }


def parts_signature(parts: list["Part"]) -> str:
    """零件内容的规范化签名；与列表顺序无关，与几何/编号/分组绑定。"""
    payload = [_panel_payload(part) for part in parts]
    payload.sort(key=lambda p: (str(p["number"]), str(p["panel_id"])))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_confirmed_drawing(
    ctx: "JobContext",
    *,
    confirmed_by: ActorKind = ActorKind.USER,
    notes: str = "",
) -> ConfirmedDrawing:
    """从当前 ctx.parts 构建不可变 ConfirmedDrawing（含父版本链）。"""
    if not ctx.parts:
        raise ValueError(
            "build_confirmed_drawing requires numbered parts; "
            "run assign_numbers first"
        )

    recognized = ctx.recognized_drawing
    if recognized is not None:
        drawing_id = recognized.drawing_id
        base_revision_id = recognized.revision_id
        source_digest = recognized.input_digest
        texts = list(recognized.texts)
    else:
        drawing_id = f"drawing_{ctx.project_id or 'adhoc'}"
        base_revision_id = f"revision_{ctx.project_id or 'adhoc'}"
        source_digest = _file_sha256(ctx.dxf_path)
        texts = []

    ctx.confirmed_revision_no += 1
    revision_no = ctx.confirmed_revision_no
    parent_digest = ctx.confirmed_drawing_digest

    profile = ctx.drawing_profile
    tolerance_mm = float(getattr(profile, "closed_tolerance", 0.01) or 0.01)

    panels = []
    for part in ctx.parts:
        payload = _panel_payload(part)
        panels.append(
            RecognizedPanel(
                panel_id=payload["panel_id"],
                geometry_wkt=payload["geometry_wkt"],
                outer_geometry_wkt=payload["outer_geometry_wkt"],
                entity_handle=payload["entity_handle"],
                number=payload["number"],
                material_group=payload["material_group"],
                holes=[
                    RecognizedHole(
                        hole_id=h["hole_id"],
                        geometry_wkt=h["geometry_wkt"],
                        entity_handle=h["entity_handle"],
                    )
                    for h in payload["holes"]
                ],
                properties=payload["properties"],
            )
        )

    note_parts = []
    if parent_digest:
        note_parts.append(f"parent_confirmed_digest:{parent_digest}")
    if notes:
        note_parts.append(notes)

    drawing = ConfirmedDrawing(
        confirmed_id="pending",
        drawing_id=drawing_id,
        revision_id=f"{base_revision_id}:c{revision_no}",
        unit=UNIT_LABELS[ctx.unit],
        tolerance_mm=tolerance_mm,
        panels=panels,
        texts=texts,
        confirmed_by=confirmed_by,
        source_digest=source_digest,
        notes=";".join(note_parts),
    )
    digest = drawing.digest(exclude=_DIGEST_EXCLUDE)
    drawing.confirmed_id = f"cd-{digest[:16]}"
    drawing.confirmed_digest = digest
    return drawing


def persist_confirmed_drawing(
    ctx: "JobContext",
    drawing: ConfirmedDrawing,
) -> Path | None:
    """内容寻址落盘到任务 artifacts 目录；无 output_dir 时跳过（仅内存）。"""
    if ctx.output_dir is None:
        return None
    store = ArtifactStore(Path(ctx.output_dir) / "artifacts")
    ref = store.put_json(
        ctx.project_id or "adhoc",
        "numbering_confirmed",
        drawing.confirmed_id,
        drawing.model_dump(mode="json"),
        stage_version=f"r{ctx.confirmed_revision_no}",
    )
    drawing.artifact_path = str(ref.path)
    return ref.path


def _file_sha256(path: str) -> str | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    return hashlib.sha256(file_path.read_bytes()).hexdigest()
