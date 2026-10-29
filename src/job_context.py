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
    # 模板命中必须显式开启：默认只用算法排板，避免复用历史布局。
    use_template: bool = False
    # 冷启动关闭 validated_cases 匹配，只用原始图纸 + 画像配置。
    use_case_library: bool = True
    manufacturing_metrics: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
    kerf_metrics: dict[str, Any] | None = None
    correction_revision: int = 0
    correction_history: list[dict[str, Any]] = field(default_factory=list)
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
