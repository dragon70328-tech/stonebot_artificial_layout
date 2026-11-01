"""In-memory state carried across one agent workflow job.

The context is deliberately free of I/O and model logic. It only holds the
deterministic intermediate values produced by trusted tools, plus the artifact
paths that later stages need to consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.constraints import NestingProfile
from src.nesting_contracts import ProcessPolicy, SheetSpec
from src.models import NestingResult
from src.units import UnitSystem


@dataclass
class JobContext:
    dxf_path: str = ""
    tenant_id: str = "default"
    project_id: str = ""
    job_id: str = ""
    unit: UnitSystem = UnitSystem.METRIC
    nesting_profile: NestingProfile | None = None
    sheet_spec: SheetSpec | None = None
    process_policy: ProcessPolicy | None = None

    width_mm: float = 0.0
    height_mm: float = 0.0
    thickness_mm: float = 20.0

    fingerprint: dict[str, Any] | None = None
    drawing_profile: Any | None = None
    drawing_profile_override: dict[str, Any] | None = None
    drawing_profile_source: str | None = None
    parts_data: list[dict[str, Any]] | None = None
    parts: list[Any] | None = None
    closed_entities: list[dict[str, Any]] = field(default_factory=list)
    classified_candidates: list[dict[str, Any]] = field(default_factory=list)
    recognized_drawing: Any | None = None
    audit_issues: list[Any] = field(default_factory=list)

    nesting_outcome: Any | None = None
    result: NestingResult | None = None
    template_result: bool = False
    template_name: str | None = None
    # 模板命中必须显式开启：默认只用算法排板，避免复用历史布局。
    use_template: bool = False
    # 冷启动关闭 validated_cases 匹配，只用原始图纸 + 画像配置。
    use_case_library: bool = True
    manufacturing_metrics: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
    kerf_metrics: dict[str, Any] | None = None
    correction_revision: int = 0
    correction_history: list[dict[str, Any]] = field(default_factory=list)
    # 编号确认后的不可变快照：排板唯一输入。digest/signature 用于在
    # nest_parts 前检测确认后的零件漂移（fail closed），revision_no 记录
    # 重复确认（修正后重确认）的版本链。
    confirmed_drawing: Any | None = None
    confirmed_drawing_digest: str | None = None
    confirmed_drawing_path: Path | None = None
    confirmed_parts_signature: str | None = None
    confirmed_revision_no: int = 0
    # 排板版本链：每次 nest_parts 追加一个不可变 NestingRevision，
    # active_revision_id 指向当前生效版本；回滚只切换指针，不覆盖历史。
    # postprocess_revision_id 记录后处理校验针对的版本，write_dxf 据此
    # 拒绝"版本已切换但校验未更新"的出图。
    nesting_revisions: list[Any] = field(default_factory=list)
    active_revision_id: str | None = None
    nesting_revision_paths: dict[str, str] = field(default_factory=dict)
    postprocess_revision_id: str | None = None
    recognition_feedback: list[dict[str, Any]] = field(default_factory=list)
    recognition_memory: dict[str, Any] = field(default_factory=dict)
    recognition_memory_path: Path | None = None
    recognition_model: dict[str, Any] = field(default_factory=dict)
    recognition_model_path: Path | None = None
    recognition_confirmation_recorded: bool = False
    recognition_confirmation_artifact_path: Path | None = None

    trials: int = 1
    seed: int = 0
    budget: float = 0.0
    quick: bool = True
    # 认证确定性模式：排板解除一切墙钟分支，结果可复现并进版本血缘。
    certified: bool = False
    pairing: bool = False
    beam: bool = True
    beam_width: int | None = None
    compress: bool = True
    rect_fast: bool = True
    low_fill_rework: bool = False
    low_fill_threshold: float = 0.6
    candidate_mode: str = "bbox"
    skip_unnumbered: bool = True

    output_dir: Path | None = None
    global_feedback_dir: Path | None = None
    numbered_dxf_path: Path | None = None
    nested_dxf_path: Path | None = None
    report_path: Path | None = None
    check_path: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe view of the current job state."""
        result = self.result
        outcome = self.nesting_outcome
        sheet_payload = {
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "thickness_mm": self.thickness_mm,
        }
        if self.sheet_spec is not None:
            sheet_payload.update(self.sheet_spec.dimensions())
        process_payload = (
            self.process_policy.model_dump(mode="json")
            if self.process_policy is not None
            else None
        )
        return {
            "dxf_path": self.dxf_path,
            "project_id": self.project_id,
            "unit": self.unit.value,
            "profile_name": (
                self.drawing_profile.name
                if self.drawing_profile is not None
                else None
            ),
            "profile_source": self.drawing_profile_source,
            "nesting_profile": (
                {
                    "processing_class": self.nesting_profile.processing_class,
                    "rotation": self.nesting_profile.rotation,
                    "min_gap": self.nesting_profile.min_gap,
                    "group_mode": self.nesting_profile.group_mode,
                }
                if self.nesting_profile is not None
                else None
            ),
            "sheet": sheet_payload,
            "process_policy": process_payload,
            "optimizer": {
                "trials": self.trials,
                "seed": self.seed,
                "budget": self.budget,
                "quick": self.quick,
                "pairing": self.pairing,
                "beam": self.beam,
                "beam_width": self.beam_width,
                "compress": self.compress,
                "rect_fast": self.rect_fast,
                "low_fill_rework": self.low_fill_rework,
                "low_fill_threshold": self.low_fill_threshold,
                "candidate_mode": self.candidate_mode,
            },
            "counts": {
                "parts_data": (
                    len(self.parts_data) if self.parts_data is not None else 0
                ),
                "parts": len(self.parts) if self.parts is not None else 0,
                "audit_issues": len(self.audit_issues),
            },
            "correction_revision": self.correction_revision,
            "correction_history": self.correction_history,
            "confirmed_drawing": (
                {
                    "confirmed_id": self.confirmed_drawing.confirmed_id,
                    "revision_id": self.confirmed_drawing.revision_id,
                    "revision_no": self.confirmed_revision_no,
                    "digest": self.confirmed_drawing_digest,
                    "parts_signature": self.confirmed_parts_signature,
                    "panels": len(self.confirmed_drawing.panels),
                    "path": (
                        str(self.confirmed_drawing_path)
                        if self.confirmed_drawing_path is not None
                        else None
                    ),
                }
                if self.confirmed_drawing is not None
                else None
            ),
            "recognition_feedback_count": len(self.recognition_feedback),
            "recognition_memory": {
                "feedback_count": int(self.recognition_memory.get("feedback_count", 0)),
                "updated_at": self.recognition_memory.get("updated_at"),
            },
            "recognition_model": {
                "model_version": self.recognition_model.get("model_version"),
                "metrics": self.recognition_model.get("metrics"),
            },
            "nesting": (
                {
                    "total_sheets": result.total_sheets,
                    "total_parts": result.total_parts,
                    "yield_rate": round(result.yield_rate, 2),
                    "elapsed_seconds": round(
                        outcome.elapsed_seconds, 1
                    ),
                    "template": self.template_result,
                    "kerf_material": self.kerf_metrics,
                }
                if result is not None and outcome is not None
                else None
            ),
            "nesting_revisions": {
                "active": self.active_revision_id,
                "count": len(self.nesting_revisions),
                "items": [
                    {
                        "revision_id": revision.revision_id,
                        "algorithm": revision.algorithm,
                        "sheets": revision.metrics.get("total_sheets"),
                        "yield_rate": revision.metrics.get("yield_rate"),
                        "elapsed_seconds": revision.metrics.get(
                            "elapsed_seconds"
                        ),
                        "validation_errors": len(revision.validation_errors),
                        "path": self.nesting_revision_paths.get(
                            revision.revision_id
                        ),
                    }
                    for revision in self.nesting_revisions
                ],
            },
            "artifacts": {
                "numbered_dxf": (
                    str(self.numbered_dxf_path)
                    if self.numbered_dxf_path is not None
                    else None
                ),
                "nested_dxf": (
                    str(self.nested_dxf_path)
                    if self.nested_dxf_path is not None
                    else None
                ),
                "report": (
                    str(self.report_path)
                    if self.report_path is not None
                    else None
                ),
                "check": (
                    str(self.check_path)
                    if self.check_path is not None
                    else None
                ),
            },
        }
