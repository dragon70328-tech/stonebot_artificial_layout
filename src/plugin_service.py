"""Business-level service used by the Agent plugin and MCP transport.

The service keeps the existing AgentRuntime and WorkflowSession as the source
of truth.  It only translates stable Agent-friendly commands into those
deterministic workflows, and deliberately does not duplicate geometry or
nesting algorithms.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.agent_contract import agent_contract
from src.agent_loop import AgentLoopResult
from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_MIN_SHEETS, PROFILES
from src.correction_contracts import CorrectionPlan
from src.drawing_corrections import build_correction_context
from src.nesting_contracts import (
    CircularSawPolicy,
    OversizeMode,
    ProcessPolicy,
    SheetSpec,
)
from src.plugin_context import (
    context_snapshot as build_context_snapshot,
    save_context_snapshot,
    restore_context_snapshot,
)
from src.plugin_delivery import (
    choose_directory_dialog,
    deliver_dxf_artifacts,
    export_word_report,
    resolve_output_directory,
)
from src.plugin_protocol import PLUGIN_VERSION, SCHEMA_VERSION, next_actions
from src.layout_adjustment import propagate_adjusted_layout
from src.licensing import (
    LicenseGuard,
    LicenseRequiredError,
    ProductionAuthorization,
)
from src.saw_audit import audit_nested_saw_clearance
from src.saw_repair import repair_nested_saw_clearance
from src.profile_learning import (
    accept_profile_patch,
    list_profile_overrides,
    list_profile_patches,
    load_profile_patch,
    matching_profile_override,
    override_root_for_project,
    propose_profile_patches,
    reject_profile_patch,
    review_profile_patch,
    rollback_profile_override,
)
from src.session_store import WorkflowSessionStore
from src.slab_texture import generate_textured_package
from src.task_capsule import (
    create_task_capsule,
    extract_task_capsule,
    inspect_task_capsule,
    remap_json_paths,
)
from src.units import UnitSystem
from src.workflow_session import WorkflowSession, WorkflowStage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class StonebotProtocolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        allowed_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.allowed_actions = allowed_actions or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "allowed_actions": self.allowed_actions,
            },
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str, fallback: str) -> str:
    safe = _SAFE_ID.sub("_", value).strip("._")
    return safe or fallback


class PluginJobService:
    """File-scoped plugin job facade around AgentRuntime."""

    def __init__(
        self,
        data_root: str | Path | None = None,
        *,
        directory_picker: Callable[[str], str | None] | None = None,
        license_guard: LicenseGuard | None = None,
    ) -> None:
        configured = os.environ.get("STONEBOT_MCP_DATA_ROOT")
        self.data_root = Path(
            data_root
            or configured
            or PROJECT_ROOT / "output" / "mcp_jobs"
        ).resolve()
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, AgentRuntime] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._directory_picker = directory_picker or choose_directory_dialog
        self.license_guard = license_guard

    # ------------------------------------------------------------------
    # Public tool methods
    # ------------------------------------------------------------------

    def analyze_drawing(self, args: dict[str, Any]) -> dict[str, Any]:
        dxf_path = Path(str(args.get("dxf_path") or "")).expanduser()
        if not dxf_path.exists():
            raise StonebotProtocolError(
                "DXF_NOT_FOUND",
                f"DXF 文件不存在: {dxf_path}",
                allowed_actions=["stonebot_analyze_drawing"],
            )
        if dxf_path.suffix.casefold() != ".dxf":
            raise StonebotProtocolError(
                "INVALID_INPUT_FORMAT",
                "当前插件只接受 DXF 图纸，请先在 CAD 中导出 DXF。",
                allowed_actions=["stonebot_analyze_drawing"],
            )

        dxf_path = dxf_path.resolve()
        tenant_id = str(args.get("tenant_id") or "default")
        project_id = str(args.get("project_id") or dxf_path.stem)
        job_id = f"stonebot-{uuid.uuid4().hex}"
        output_dir = self.jobs_root / job_id / "run"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "dxf_path": str(dxf_path),
            "output_dir": str(output_dir),
            "initial": {
                "skip_unnumbered": bool(args.get("skip_unnumbered", True)),
                "cold_start": bool(args.get("cold_start", False)),
                "use_template": bool(args.get("use_template", False)),
            },
            "nesting": None,
            "nesting_configured": False,
            "correction_plans": [],
            "context_snapshot_version": 1,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "stage": WorkflowStage.UPLOADED.value,
        }
        self._manifests[job_id] = manifest
        runtime = self._make_runtime(manifest)
        safe_tenant = _safe_id(tenant_id, "default")
        safe_project = _safe_id(project_id, "project")
        override = matching_profile_override(
            dxf_path,
            override_root_for_project(
                self.data_root,
                safe_tenant,
                safe_project,
            ),
        )
        if override is not None:
            profile_override, meta = override
            runtime.context.drawing_profile_override = profile_override
            runtime.context.drawing_profile_source = (
                f"project_override:{meta['patch_id']}"
            )
        self._jobs[job_id] = runtime
        self._save_manifest(manifest)

        runtime.run_next()
        self._sync_manifest(job_id, runtime)
        return self._public_state(runtime, manifest)

    def configure_nesting(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(
            runtime,
            {
                WorkflowStage.AUDITED,
                WorkflowStage.NESTED_REPORTED,
                WorkflowStage.POSTPROCESS_CONFIRMED,
            },
        )
        previous_stage = runtime.session.stage
        previous_config = dict(manifest.get("nesting") or {})

        config = self._normalize_nesting_config(args)
        self._apply_nesting_config(runtime, config)
        manifest["nesting"] = config
        manifest["nesting_configured"] = True

        renested = previous_stage != WorkflowStage.AUDITED
        if renested:
            try:
                runtime.renest()
            except Exception as exc:  # noqa: BLE001 - service boundary
                # 重排失败：把配置退回上一版，会话与旧排板结果原样可用。
                if previous_config.get("width_mm") and previous_config.get(
                    "height_mm"
                ):
                    self._apply_nesting_config(runtime, previous_config)
                    manifest["nesting"] = previous_config
                raise StonebotProtocolError(
                    "RENEST_FAILED",
                    f"按新条件重排失败：{exc}",
                    details={
                        "stage": runtime.session.current_stage,
                        "nesting_revisions": len(
                            runtime.context.nesting_revisions
                        ),
                    },
                    allowed_actions=[
                        "stonebot_configure_nesting",
                        "stonebot_rollback_nesting",
                        "stonebot_job_status",
                    ],
                ) from exc
        self._sync_manifest(job_id, runtime)
        state = self._public_state(runtime, manifest)
        if renested:
            revisions = runtime.context.nesting_revisions
            state["renested"] = True
            state["previous_stage"] = previous_stage.value
            state["new_revision"] = (
                revisions[-1].revision_id if revisions else None
            )
        return state

    def list_drawing_issues(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, _manifest = self._runtime_and_manifest(job_id)
        issue_type = str(args.get("type") or "").strip().casefold()
        severity = str(args.get("severity") or "").strip().casefold()
        offset = max(0, int(args.get("offset", 0)))
        limit = min(200, max(1, int(args.get("limit", 50))))

        issues = list(runtime.context.audit_issues)
        if issue_type:
            issues = [
                issue
                for issue in issues
                if str(issue.type).casefold() == issue_type
            ]
        if severity:
            issues = [
                issue
                for issue in issues
                if str(issue.severity).casefold() == severity
            ]
        page = [
            issue.to_dict()
            for issue in issues[offset:offset + limit]
        ]
        return {
            "ok": True,
            "job_id": job_id,
            "stage": runtime.session.current_stage,
            "total": len(issues),
            "offset": offset,
            "limit": limit,
            "items": page,
        }

    def list_correction_targets(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, _manifest = self._runtime_and_manifest(job_id)
        if not runtime.context.parts:
            raise StonebotProtocolError(
                "DRAWING_NOT_READY",
                "当前任务尚未完成读图和编号，不能列出纠正目标。",
                allowed_actions=["stonebot_job_status"],
            )

        context = build_correction_context(runtime.context)
        query = str(args.get("query") or "").strip().casefold()
        include_unrecognized = bool(
            args.get("include_unrecognized", True)
        )
        offset = max(0, int(args.get("offset", 0)))
        limit = min(200, max(1, int(args.get("limit", 50))))

        parts = context.get("parts", [])
        unrecognized = (
            context.get("unrecognized_entities", [])
            if include_unrecognized
            else []
        )
        combined = [
            {**item, "kind": "recognized_target"}
            for item in parts
        ] + [
            {**item, "kind": "unrecognized_entity"}
            for item in unrecognized
        ]
        if query:
            combined = [
                item
                for item in combined
                if query in json.dumps(
                    item,
                    ensure_ascii=False,
                    default=str,
                ).casefold()
            ]
        return {
            "ok": True,
            "job_id": job_id,
            "stage": runtime.session.current_stage,
            "correction_revision": runtime.context.correction_revision,
            "total": len(combined),
            "recognized_target_count": len(parts),
            "unrecognized_entity_count": len(
                context.get("unrecognized_entities", [])
            ),
            "offset": offset,
            "limit": limit,
            "items": combined[offset:offset + limit],
        }

    def correct_drawing(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        plan_payload = args.get("plan")
        if not isinstance(plan_payload, dict):
            raise StonebotProtocolError(
                "INVALID_CORRECTION_PLAN",
                "plan 必须是结构化 CorrectionPlan 对象。",
            )
        try:
            plan = CorrectionPlan.model_validate(plan_payload)
        except Exception as exc:  # noqa: BLE001 - validation boundary
            raise StonebotProtocolError(
                "INVALID_CORRECTION_PLAN",
                f"纠正计划校验失败: {exc}",
            ) from exc
        try:
            runtime.correct(plan)
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "CORRECTION_FAILED",
                str(exc),
                allowed_actions=[
                    "stonebot_list_correction_targets",
                    "stonebot_job_status",
                ],
            ) from exc
        manifest.setdefault("correction_plans", []).append(
            plan.model_dump(mode="json")
        )
        patches = propose_profile_patches(
            runtime.context,
            data_root=self.data_root,
            tenant_id=_safe_id(
                str(manifest.get("tenant_id") or "default"),
                "default",
            ),
            project_id=_safe_id(
                str(manifest.get("project_id") or "project"),
                "project",
            ),
        )
        self._sync_manifest(job_id, runtime)
        state = self._public_state(runtime, manifest)
        state["profile_patches"] = patches
        return state

    def rename_panels(self, args: dict[str, Any]) -> dict[str, Any]:
        renames = args.get("renames")
        if not isinstance(renames, list) or not renames:
            raise StonebotProtocolError(
                "RENAMES_REQUIRED",
                "renames 必须是非空列表。",
            )
        operations = []
        for item in renames:
            if not isinstance(item, dict):
                raise StonebotProtocolError(
                    "INVALID_RENAMES",
                    "renames 的每一项必须是对象。",
                )
            target_id = str(item.get("target_id") or "").strip()
            new_number = str(item.get("new_number") or "").strip()
            if not target_id or not new_number:
                raise StonebotProtocolError(
                    "INVALID_RENAMES",
                    "target_id 和 new_number 不能为空。",
                )
            operations.append(
                {
                    "operation": "rename_number",
                    "target": target_id,
                    "value": new_number,
                    "reason": "通过 rename_panels 修正编号",
                }
            )
        return self.correct_drawing(
            {
                "job_id": args.get("job_id"),
                "plan": {
                    "instruction": str(
                        args.get("instruction") or "按用户要求修正编号"
                    ),
                    "operations": operations,
                    "created_by": "ai",
                    "feedback_scope": "project",
                },
            }
        )

    def confirm_drawing(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(runtime, {WorkflowStage.AUDITED})
        if not manifest.get("nesting_configured"):
            raise StonebotProtocolError(
                "NESTING_NOT_CONFIGURED",
                "确认图纸前必须先配置大板尺寸、加工方式和排板参数。",
                allowed_actions=["stonebot_configure_nesting"],
            )
        self._confirm(runtime, str(args.get("decision") or ""))
        self._sync_manifest(job_id, runtime)
        return self._public_state(runtime, manifest)

    def confirm_nesting(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(runtime, {WorkflowStage.NESTED_REPORTED})
        self._confirm(runtime, str(args.get("decision") or ""))
        self._sync_manifest(job_id, runtime)
        return self._public_state(runtime, manifest)

    def rollback_nesting(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(
            runtime,
            {WorkflowStage.NESTED_REPORTED, WorkflowStage.POSTPROCESS_CONFIRMED},
        )
        revision_id = str(args.get("revision_id") or "").strip()
        if not revision_id:
            raise StonebotProtocolError(
                "REVISION_ID_REQUIRED",
                "revision_id 不能为空；可用版本见 nesting_revisions。",
                allowed_actions=["stonebot_job_status"],
            )
        try:
            output = runtime.rollback_nesting(revision_id)
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "ROLLBACK_FAILED",
                str(exc),
                allowed_actions=["stonebot_job_status"],
            ) from exc
        self._sync_manifest(job_id, runtime)
        state = self._public_state(runtime, manifest)
        state["rollback"] = output
        return state

    def choose_output_directory(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        purpose = str(args.get("purpose") or "delivery").strip()
        titles = {
            "delivery": "选择原位 DXF 和排板 DXF 的保存文件夹",
            "word_report": "选择 Word 排板报告的保存文件夹",
            "texture": "选择人造石纹理交付包的空文件夹",
        }
        title = titles.get(purpose, titles["delivery"])
        try:
            selected = self._directory_picker(title)
        except Exception as exc:  # noqa: BLE001 - desktop boundary
            raise StonebotProtocolError(
                "DIRECTORY_PICKER_UNAVAILABLE",
                f"无法打开保存目录选择器: {exc}",
                details={"purpose": purpose},
            ) from exc
        return {
            "ok": True,
            "canceled": selected is None,
            "output_directory": selected or "",
            "purpose": purpose,
        }

    def finalize_job(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        output_directory = str(
            args.get("output_directory") or ""
        ).strip()
        if not output_directory:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_REQUIRED",
                "最终出图前必须选择保存文件夹。",
                allowed_actions=["stonebot_choose_output_directory"],
            )
        overwrite = bool(args.get("overwrite", False))
        try:
            target_dir = resolve_output_directory(output_directory)
            summary_name = f"{Path(runtime.dxf_path).stem}_材料统计.xlsx"
            delivery_sources = [
                runtime.context.numbered_dxf_path,
                runtime.nested_path,
            ]
            candidate_targets = [
                target_dir / Path(source).name
                for source in delivery_sources
                if source
            ]
            candidate_targets.append(target_dir / summary_name)
            collisions = [
                str(path)
                for path in candidate_targets
                if path.exists()
                and not overwrite
            ]
            if collisions:
                raise FileExistsError(
                    "保存目录已有同名文件: "
                    + ", ".join(collisions)
                )
        except (OSError, ValueError) as exc:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_INVALID",
                str(exc),
                allowed_actions=[
                    "stonebot_choose_output_directory",
                ],
            ) from exc
        delivery = manifest.get("delivery")
        if (
            runtime.session.stage == WorkflowStage.COMPLETED
            and delivery
            and all(
                Path(path).is_file()
                for path in delivery.get("artifacts", {}).values()
            )
        ):
            self._retry_pending_license_commit(manifest)
            return self._public_state(runtime, manifest)
        authorization = None
        if self.license_guard is not None:
            try:
                input_sha256 = None
                recognized_drawing = getattr(
                    runtime.context,
                    "recognized_drawing",
                    None,
                )
                if recognized_drawing is not None:
                    input_sha256 = (
                        recognized_drawing.input_digest
                    )
                source_path = Path(runtime.dxf_path)
                if input_sha256 is None and source_path.is_file():
                    input_sha256 = hashlib.sha256(
                        source_path.read_bytes()
                    ).hexdigest()
                authorization = self.license_guard.begin_production(
                    job_id=job_id,
                    input_sha256=input_sha256,
                )
            except LicenseRequiredError as exc:
                raise StonebotProtocolError(
                    "LICENSE_REQUIRED",
                    "当前设备没有可用于生产交付的有效许可。",
                    details=exc.status.to_dict(),
                    allowed_actions=[
                        "stonebot_license_status",
                        "stonebot_job_status",
                    ],
                ) from exc
        try:
            if runtime.session.stage != WorkflowStage.COMPLETED:
                self._require_stage(
                    runtime,
                    {WorkflowStage.POSTPROCESS_CONFIRMED},
                )
                self._confirm(runtime, str(args.get("decision") or ""))
                self._sync_manifest(job_id, runtime)
            delivered = deliver_dxf_artifacts(
                numbered_source=(
                    runtime.context.numbered_dxf_path or ""
                ),
                nested_source=runtime.context.nested_dxf_path or "",
                output_directory=output_directory,
                overwrite=overwrite,
            )
        except Exception as exc:  # noqa: BLE001 - delivery boundary
            if self.license_guard is not None:
                self.license_guard.release_production(authorization)
            raise StonebotProtocolError(
                "OUTPUT_DELIVERY_FAILED",
                str(exc),
                details={
                    "job_id": job_id,
                    "output_directory": output_directory,
                },
                allowed_actions=[
                    "stonebot_choose_output_directory",
                    "stonebot_job_status",
                ],
            ) from exc
        if self.license_guard is not None and authorization is not None:
            try:
                self.license_guard.commit_production(authorization)
            except Exception as exc:  # noqa: BLE001 - licensing boundary
                manifest["license_usage_pending"] = {
                    "authorization_id": authorization.authorization_id,
                    "subscription": authorization.subscription,
                    "error": str(exc),
                }
        try:
            from src.material_summary import write_material_summary_xlsx

            summary_path = write_material_summary_xlsx(
                runtime.context.parts,
                target_dir / summary_name,
            )
            delivered["artifacts"]["material_summary"] = str(summary_path)
            delivered["file_names"].append(summary_path.name)
        except Exception as exc:  # noqa: BLE001 - 统计表失败不阻断 DXF 交付
            delivered["material_summary_error"] = str(exc)
        manifest["delivery"] = {
            **delivered,
            "delivered_at": _utc_now(),
        }
        self._save_manifest(manifest)
        return self._public_state(runtime, manifest)

    def _retry_pending_license_commit(
        self,
        manifest: dict[str, Any],
    ) -> None:
        pending = manifest.get("license_usage_pending")
        if self.license_guard is None or not pending:
            return
        authorization_id = str(pending.get("authorization_id") or "")
        if not authorization_id:
            manifest.pop("license_usage_pending", None)
            return
        try:
            self.license_guard.commit_production(
                ProductionAuthorization(
                    authorization_id=authorization_id,
                    subscription=bool(pending.get("subscription", False)),
                )
            )
        except Exception as exc:  # noqa: BLE001 - retry remains pending
            pending["error"] = str(exc)
        else:
            manifest.pop("license_usage_pending", None)
        self._save_manifest(manifest)

    def generate_textured_drawing(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(runtime, {WorkflowStage.COMPLETED})
        manifest_path = Path(str(
            args.get("pattern_manifest_path") or ""
        )).expanduser().resolve()
        if not manifest_path.is_file():
            raise StonebotProtocolError(
                "TEXTURE_MANIFEST_NOT_FOUND",
                f"人造石图案清单不存在: {manifest_path}",
                allowed_actions=["stonebot_generate_textured_drawing"],
            )
        output_directory = str(args.get("output_directory") or "").strip()
        if not output_directory:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_REQUIRED",
                "生成纹理包前必须选择一个空文件夹。",
                allowed_actions=["stonebot_choose_output_directory"],
            )
        if (
            runtime.context.result is None
            or runtime.context.numbered_dxf_path is None
        ):
            raise StonebotProtocolError(
                "TEXTURE_CONTEXT_MISSING",
                "当前任务缺少排板结果或原位编号 DXF。",
            )
        try:
            target_dir = resolve_output_directory(output_directory)
            package = generate_textured_package(
                runtime.context.result,
                runtime.context.numbered_dxf_path,
                manifest_path,
                target_dir,
                image_reference_mode=str(
                    args.get("image_reference_mode") or "absolute"
                ),
                create_archive=bool(args.get("archive", False)),
            )
        except Exception as exc:  # noqa: BLE001 - deterministic texture boundary
            raise StonebotProtocolError(
                "TEXTURE_GENERATION_FAILED",
                str(exc),
                details={
                    "job_id": job_id,
                    "pattern_manifest_path": str(manifest_path),
                },
                allowed_actions=[
                    "stonebot_choose_output_directory",
                    "stonebot_job_status",
                ],
            ) from exc
        manifest["texture_package"] = {
            "output_directory": str(package.output_dir),
            "textured_dxf": str(package.textured_dxf),
            "manifest": str(package.manifest_path),
            "portable_dxf": (
                str(package.portable_dxf)
                if package.portable_dxf is not None
                else None
            ),
            "archive": (
                str(package.archive_path)
                if package.archive_path is not None
                else None
            ),
            "texture_count": package.texture_count,
            "preview_paths": [
                str(path) for path in package.preview_paths
            ],
            "created_at": _utc_now(),
        }
        self._save_manifest(manifest)
        state = self._public_state(runtime, manifest)
        state["texture_package"] = manifest["texture_package"]
        return state

    def propagate_layout_adjustment(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Propagate one user-adjusted sheet to identical board inventories."""
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(runtime, {WorkflowStage.COMPLETED})
        adjusted_path = Path(
            str(args.get("adjusted_dxf_path") or "")
        ).expanduser().resolve()
        if not adjusted_path.is_file():
            raise StonebotProtocolError(
                "ADJUSTED_DXF_NOT_FOUND",
                f"调整后的排板 DXF 不存在: {adjusted_path}",
                allowed_actions=["stonebot_propagate_layout_adjustment"],
            )
        output_directory = str(
            args.get("output_directory") or ""
        ).strip()
        if not output_directory:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_REQUIRED",
                "传播布局前必须选择一个保存文件夹。",
                allowed_actions=["stonebot_choose_output_directory"],
            )
        try:
            output_dir = resolve_output_directory(output_directory)
            output_dxf = (
                output_dir
                / f"{adjusted_path.stem}_布局调整传播.dxf"
            )
            output_manifest = output_dxf.with_suffix(".json")
            overwrite = bool(args.get("overwrite", False))
            if output_dxf.exists() and not overwrite:
                raise FileExistsError(f"目标文件已存在: {output_dxf}")
            if output_manifest.exists() and not overwrite:
                raise FileExistsError(
                    f"目标清单已存在: {output_manifest}"
                )
            propagated = propagate_adjusted_layout(
                adjusted_path,
                int(args["reference_sheet"]),
                output_dxf,
            )
            payload = propagated.as_dict()
            output_manifest.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "LAYOUT_ADJUSTMENT_FAILED",
                f"布局调整传播失败：{exc}",
                details={
                    "adjusted_dxf_path": str(adjusted_path),
                    "reference_sheet": args.get("reference_sheet"),
                },
                allowed_actions=[
                    "stonebot_propagate_layout_adjustment",
                    "stonebot_job_status",
                ],
            ) from exc

        manifest["layout_adjustment"] = {
            **payload,
            "adjustment_manifest": str(output_manifest),
            "created_at": _utc_now(),
        }
        self._save_manifest(manifest)
        state = self._public_state(runtime, manifest)
        state["layout_adjustment"] = manifest["layout_adjustment"]
        return state

    def audit_saw_clearance(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Audit an existing nested DXF for neighbour-cutting risks."""
        job_id = self._require_job_id(args)
        runtime, _manifest = self._runtime_and_manifest(job_id)
        self._require_stage(
            runtime,
            {
                WorkflowStage.NESTED_REPORTED,
                WorkflowStage.POSTPROCESS_CONFIRMED,
                WorkflowStage.COMPLETED,
            },
        )
        nested_path = Path(
            str(args.get("nested_dxf_path") or runtime.nested_path or "")
        ).expanduser().resolve()
        if not nested_path.is_file():
            raise StonebotProtocolError(
                "NESTED_DXF_NOT_FOUND",
                f"排板 DXF 不存在: {nested_path}",
                allowed_actions=["stonebot_audit_saw_clearance"],
            )
        try:
            report = audit_nested_saw_clearance(
                nested_path,
                diameter_mm=float(args["diameter_mm"]),
                thickness_mm=float(
                    args.get("thickness_mm") or runtime.context.thickness_mm
                ),
                plunge_extra_mm=float(
                    args.get("plunge_extra_mm", 2.0)
                ),
                safety_margin_mm=float(
                    args.get("safety_margin_mm", 0.0)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - audit boundary
            raise StonebotProtocolError(
                "SAW_AUDIT_FAILED",
                f"圆盘锯碰撞审计失败：{exc}",
                details={"nested_dxf_path": str(nested_path)},
                allowed_actions=["stonebot_audit_saw_clearance"],
            ) from exc
        return {
            "audit_completed": True,
            "safe": report["safe"],
            "audit": report,
        }

    def repair_saw_clearance(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Repair an existing nested DXF without changing its sheet count."""
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(
            runtime,
            {
                WorkflowStage.NESTED_REPORTED,
                WorkflowStage.POSTPROCESS_CONFIRMED,
                WorkflowStage.COMPLETED,
            },
        )
        nested_path = Path(
            str(args.get("nested_dxf_path") or runtime.nested_path or "")
        ).expanduser().resolve()
        if not nested_path.is_file():
            raise StonebotProtocolError(
                "NESTED_DXF_NOT_FOUND",
                f"排板 DXF 不存在: {nested_path}",
                allowed_actions=["stonebot_repair_saw_clearance"],
            )
        output_directory = str(
            args.get("output_directory") or ""
        ).strip()
        if not output_directory:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_REQUIRED",
                "修复让刀碰撞前必须选择保存文件夹。",
                allowed_actions=["stonebot_choose_output_directory"],
            )
        try:
            target_dir = resolve_output_directory(output_directory)
            output_dxf = target_dir / f"{nested_path.stem}_让刀修复.dxf"
            output_manifest = output_dxf.with_suffix(".json")
            overwrite = bool(args.get("overwrite", False))
            if output_dxf.exists() and not overwrite:
                raise FileExistsError(f"目标文件已存在: {output_dxf}")
            if output_manifest.exists() and not overwrite:
                raise FileExistsError(
                    f"目标清单已存在: {output_manifest}"
                )
            repaired = repair_nested_saw_clearance(
                nested_path,
                output_dxf,
                diameter_mm=float(args["diameter_mm"]),
                thickness_mm=float(
                    args.get("thickness_mm") or runtime.context.thickness_mm
                ),
                plunge_extra_mm=float(
                    args.get("plunge_extra_mm", 2.0)
                ),
                safety_margin_mm=float(
                    args.get("safety_margin_mm", 0.0)
                ),
                min_gap_mm=float(args.get("min_gap_mm", 0.0)),
                max_moves_per_sheet=int(
                    args.get("max_moves_per_sheet", 12)
                ),
            )
            payload = repaired.as_dict()
            output_manifest.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "SAW_REPAIR_FAILED",
                f"圆盘锯让刀修复失败：{exc}",
                details={"nested_dxf_path": str(nested_path)},
                allowed_actions=[
                    "stonebot_repair_saw_clearance",
                    "stonebot_audit_saw_clearance",
                ],
            ) from exc
        manifest["saw_repair"] = {
            **payload,
            "repair_manifest": str(output_manifest),
            "created_at": _utc_now(),
        }
        self._save_manifest(manifest)
        state = self._public_state(runtime, manifest)
        state["saw_repair"] = manifest["saw_repair"]
        return state

    def export_word_report(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._require_stage(runtime, {WorkflowStage.COMPLETED})
        output_directory = str(
            args.get("output_directory") or ""
        ).strip()
        if not output_directory:
            raise StonebotProtocolError(
                "OUTPUT_DIRECTORY_REQUIRED",
                "生成 Word 报告前必须选择保存文件夹。",
                allowed_actions=["stonebot_choose_output_directory"],
            )
        try:
            path = export_word_report(
                runtime.context,
                job_id=job_id,
                output_directory=output_directory,
                overwrite=bool(args.get("overwrite", False)),
            )
        except Exception as exc:  # noqa: BLE001 - report boundary
            raise StonebotProtocolError(
                "WORD_REPORT_EXPORT_FAILED",
                str(exc),
                details={"job_id": job_id},
                allowed_actions=[
                    "stonebot_choose_output_directory",
                    "stonebot_job_status",
                ],
            ) from exc
        manifest["word_report"] = {
            "path": path,
            "created_at": _utc_now(),
        }
        self._save_manifest(manifest)
        state = self._public_state(runtime, manifest)
        state["word_report"] = path
        return state

    def advance_until_gate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Compact-profile command using one explicit human gate decision."""
        gate = str(args.get("gate") or "").strip().casefold()
        handlers = {
            "drawing": self.confirm_drawing,
            "nesting": self.confirm_nesting,
            "final": self.finalize_job,
        }
        handler = handlers.get(gate)
        if handler is None:
            raise StonebotProtocolError(
                "INVALID_GATE",
                "gate 必须是 drawing、nesting 或 final。",
                allowed_actions=["stonebot_job_status"],
            )
        return handler(
            {
                "job_id": args.get("job_id"),
                "decision": args.get("decision"),
                "output_directory": args.get("output_directory"),
                "overwrite": args.get("overwrite", False),
            }
        )

    def get_agent_contract(self, args: dict[str, Any]) -> dict[str, Any]:
        profile = str(args.get("profile") or "full").casefold()
        try:
            payload = agent_contract(profile)
        except ValueError as exc:
            raise StonebotProtocolError(
                "UNKNOWN_TOOL_PROFILE",
                str(exc),
                allowed_actions=["stonebot_get_agent_contract"],
            ) from exc
        return {"ok": True, **payload}

    def list_profile_patches(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        patches = list_profile_patches(
            self.data_root,
            tenant_id=args.get("tenant_id"),
            project_id=args.get("project_id"),
            status=args.get("status"),
        )
        limit = min(200, max(1, int(args.get("limit", 50))))
        return {
            "ok": True,
            "total": len(patches),
            "items": patches[:limit],
        }

    def accept_profile_patch(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        patch_id = str(args.get("patch_id") or "").strip()
        if not patch_id:
            raise StonebotProtocolError(
                "PATCH_ID_REQUIRED",
                "缺少 patch_id。",
            )
        try:
            patch = load_profile_patch(self.data_root, patch_id)
            job_id = str(patch["evidence"]["job_id"])
            manifest = self._load_manifest(job_id)
            accepted = accept_profile_patch(
                self.data_root,
                patch_id=patch_id,
                source_path=manifest["dxf_path"],
                profiles_root=PROJECT_ROOT / "drawing_profiles",
            )
        except Exception as exc:  # noqa: BLE001 - validation boundary
            raise StonebotProtocolError(
                "PROFILE_PATCH_REJECTED",
                str(exc),
                allowed_actions=["stonebot_list_profile_patches"],
            ) from exc
        return {"ok": True, "patch": accepted}

    def review_profile_patch(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        patch_id = str(args.get("patch_id") or "").strip()
        if not patch_id:
            raise StonebotProtocolError(
                "PATCH_ID_REQUIRED",
                "缺少 patch_id。",
            )
        try:
            patch = load_profile_patch(self.data_root, patch_id)
            manifest = self._load_manifest(
                str(patch["evidence"]["job_id"])
            )
            reviewed = review_profile_patch(
                self.data_root,
                patch_id=patch_id,
                source_path=manifest["dxf_path"],
                profiles_root=PROJECT_ROOT / "drawing_profiles",
            )
        except Exception as exc:  # noqa: BLE001 - validation boundary
            raise StonebotProtocolError(
                "PROFILE_PATCH_REVIEW_FAILED",
                str(exc),
                allowed_actions=["stonebot_list_profile_patches"],
            ) from exc
        return {"ok": True, "patch": reviewed}

    def reject_profile_patch(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        patch_id = str(args.get("patch_id") or "").strip()
        if not patch_id:
            raise StonebotProtocolError(
                "PATCH_ID_REQUIRED",
                "缺少 patch_id。",
            )
        try:
            patch = reject_profile_patch(
                self.data_root,
                patch_id,
            )
        except FileNotFoundError as exc:
            raise StonebotProtocolError(
                "PROFILE_PATCH_NOT_FOUND",
                str(exc),
            ) from exc
        return {"ok": True, "patch": patch}

    def list_profile_overrides(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        tenant_id = str(args.get("tenant_id") or "default")
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            raise StonebotProtocolError(
                "PROJECT_ID_REQUIRED",
                "缺少 project_id。",
            )
        payload = list_profile_overrides(
            self.data_root,
            tenant_id=_safe_id(tenant_id, "default"),
            project_id=_safe_id(project_id, "project"),
            base_profile_name=args.get("base_profile_name"),
        )
        return {"ok": True, **payload}

    def rollback_profile_override(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        tenant_id = str(args.get("tenant_id") or "default")
        project_id = str(args.get("project_id") or "").strip()
        base_profile_name = str(
            args.get("base_profile_name") or ""
        ).strip()
        if not project_id or not base_profile_name:
            raise StonebotProtocolError(
                "PROFILE_OVERRIDE_TARGET_REQUIRED",
                "project_id 和 base_profile_name 均为必填。",
            )
        try:
            payload = rollback_profile_override(
                self.data_root,
                tenant_id=_safe_id(tenant_id, "default"),
                project_id=_safe_id(project_id, "project"),
                base_profile_name=base_profile_name,
                target_patch_id=args.get("target_patch_id"),
            )
        except Exception as exc:  # noqa: BLE001 - persistence boundary
            raise StonebotProtocolError(
                "PROFILE_OVERRIDE_ROLLBACK_FAILED",
                str(exc),
                allowed_actions=["stonebot_list_profile_overrides"],
            ) from exc
        return {"ok": True, **payload}

    def job_status(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime = self._jobs.get(job_id)
        if runtime is not None:
            return self._public_state(runtime, self._manifests[job_id])
        manifest = self._load_manifest(job_id)
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "stage": manifest.get("stage"),
            "confirmation_required": manifest.get("stage") in {
                WorkflowStage.AUDITED.value,
                WorkflowStage.NESTED_REPORTED.value,
                WorkflowStage.POSTPROCESS_CONFIRMED.value,
            },
            "nesting_configured": bool(
                manifest.get("nesting_configured")
            ),
            "summary": manifest.get("last_summary", {}),
            "next_actions": next_actions(
                str(manifest.get("stage") or ""),
                nesting_configured=bool(
                    manifest.get("nesting_configured")
                ),
            ),
            "context_available": self._context_path(job_id).exists(),
            "message": (
                "任务元数据和几何快照可恢复；确认、纠正或排板时"
                "将自动载入上下文。"
                if self._context_path(job_id).exists()
                else "任务元数据存在，但几何快照缺失。"
            ),
        }

    def list_jobs(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        tenant_id = str((args or {}).get("tenant_id") or "").strip()
        jobs: list[dict[str, Any]] = []
        for path in sorted(self.jobs_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if tenant_id and manifest.get("tenant_id") != tenant_id:
                continue
            jobs.append(
                {
                    "job_id": manifest.get("job_id"),
                    "tenant_id": manifest.get("tenant_id"),
                    "project_id": manifest.get("project_id"),
                    "stage": manifest.get("stage"),
                    "updated_at": manifest.get("updated_at"),
                }
            )
        return {"ok": True, "total": len(jobs), "jobs": jobs}

    def cancel_job(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_job_id(args)
        runtime, manifest = self._runtime_and_manifest(job_id)
        try:
            runtime.cancel()
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "CANCEL_FAILED",
                str(exc),
                allowed_actions=["stonebot_job_status"],
            ) from exc
        self._sync_manifest(job_id, runtime)
        return self._public_state(runtime, manifest)

    def license_status(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        status = (
            self.license_guard.status()
            if self.license_guard is not None
            else None
        )
        return {
            "ok": True,
            "enforced": self.license_guard is not None,
            "license": (
                status.to_dict()
                if status is not None
                else {
                    "state": "disabled",
                    "valid": True,
                    "production_allowed": True,
                    "reason": "no license guard configured",
                }
            ),
        }

    def export_task_capsule(self, args: dict[str, Any]) -> dict[str, Any]:
        """Export the complete resumable job state as a portable capsule."""
        job_id = self._require_job_id(args)
        raw_destination = str(
            args.get("destination") or args.get("capsule_path") or ""
        ).strip()
        if not raw_destination:
            raise StonebotProtocolError(
                "CAPSULE_DESTINATION_REQUIRED",
                "导出任务胶囊必须提供 destination。",
                allowed_actions=["stonebot_export_task_capsule"],
            )
        destination = Path(raw_destination).expanduser()
        runtime, manifest = self._runtime_and_manifest(job_id)
        self._sync_manifest(job_id, runtime)

        source_path = Path(
            runtime.context.dxf_path or manifest.get("dxf_path") or ""
        )
        if not source_path.is_file():
            raise StonebotProtocolError(
                "CAPSULE_INPUT_MISSING",
                f"任务输入 DXF 不存在，不能生成可迁移胶囊: {source_path}",
                allowed_actions=["stonebot_job_status"],
            )

        files: dict[str, Path] = {"input_dxf": source_path}

        def add_file(role: str, raw_path: Any) -> None:
            if not raw_path:
                return
            candidate = Path(str(raw_path))
            if candidate.is_file():
                files[role] = candidate

        for role, raw_path in (
            ("numbered_dxf", runtime.context.numbered_dxf_path),
            ("nested_dxf", runtime.context.nested_dxf_path),
            ("report", runtime.context.report_path),
            ("check", runtime.context.check_path),
            (
                "recognition_confirmation",
                runtime.context.recognition_confirmation_artifact_path,
            ),
        ):
            add_file(role, raw_path)

        if runtime.context.confirmed_drawing is not None:
            add_file(
                "confirmed_drawing",
                runtime.context.confirmed_drawing.artifact_path,
            )
        for revision in runtime.context.nesting_revisions or []:
            revision_id = str(revision.revision_id)
            add_file(
                f"nesting_revision_{_safe_id(revision_id, 'revision')}",
                runtime.context.nesting_revision_paths.get(revision_id),
            )
        for role, raw_path in (
            (manifest.get("delivery") or {}).get("artifacts") or {}
        ).items():
            add_file(f"delivery_{_safe_id(str(role), 'artifact')}", raw_path)
        add_file("word_report", (manifest.get("word_report") or {}).get("path"))
        for role, raw_path in (
            manifest.get("texture_package") or {}
        ).items():
            add_file(f"texture_{_safe_id(str(role), 'artifact')}", raw_path)

        capsule_path = create_task_capsule(
            destination,
            job_manifest=manifest,
            context_snapshot=build_context_snapshot(runtime.context),
            files=files,
            state_payloads={
                "workflow_session": runtime.session.to_dict(),
            },
            runtime_metadata={
                "plugin_version": PLUGIN_VERSION,
                "schema_version": SCHEMA_VERSION,
                "active_revision_id": runtime.context.active_revision_id,
                "certified": bool(runtime.context.certified),
            },
        )
        info = inspect_task_capsule(capsule_path)
        return {
            "ok": True,
            "job_id": job_id,
            "capsule_path": str(capsule_path),
            "capsule_id": info.manifest.get("capsule_id"),
            "capsule_digest": info.manifest.get("capsule_digest"),
            "stage": info.stage,
            "file_count": len(info.files),
            "total_bytes": info.manifest.get("total_bytes"),
        }

    def import_task_capsule(self, args: dict[str, Any]) -> dict[str, Any]:
        """Import a verified capsule into this Runtime's local job store."""
        raw_capsule = str(args.get("capsule_path") or "")
        if not raw_capsule:
            raise StonebotProtocolError(
                "CAPSULE_PATH_REQUIRED",
                "导入任务胶囊必须提供 capsule_path。",
                allowed_actions=["stonebot_import_task_capsule"],
            )
        capsule_path = Path(raw_capsule).expanduser().resolve()
        try:
            info = inspect_task_capsule(capsule_path)
        except Exception as exc:  # noqa: BLE001 - untrusted archive boundary
            raise StonebotProtocolError(
                "CAPSULE_INVALID",
                f"任务胶囊校验失败: {exc}",
                allowed_actions=["stonebot_import_task_capsule"],
            ) from exc
        job_id = info.job_id
        if not job_id:
            raise StonebotProtocolError(
                "CAPSULE_INVALID",
                "任务胶囊缺少 job_id。",
            )
        job_dir = self.jobs_root / _safe_id(job_id, "invalid")
        if job_dir.exists():
            raise StonebotProtocolError(
                "CAPSULE_JOB_EXISTS",
                f"本机已经存在同 ID 任务: {job_id}",
                allowed_actions=["stonebot_job_status", "stonebot_list_jobs"],
            )

        try:
            imported = extract_task_capsule(capsule_path, job_dir)
            manifest_payload = json.loads(
                imported.manifest_path.read_text(encoding="utf-8")
            )
            context_path = imported.root / "state" / "context_snapshot.json"
            context_payload = json.loads(
                context_path.read_text(encoding="utf-8")
            )
            session_path = imported.state.get("workflow_session")
            if session_path is None:
                raise ValueError("workflow_session state is missing")
            session_payload = json.loads(
                session_path.read_text(encoding="utf-8")
            )
            session = WorkflowSession.from_dict(session_payload)
            if session.session_id != job_id:
                raise ValueError(
                    "session_id does not match capsule job_id"
                )
            input_path = imported.files.get("input_dxf")
            if input_path is None or not input_path.is_file():
                raise ValueError("input_dxf file is missing")

            output_dir = job_dir / "run"
            replacements: dict[str, str | Path] = {}
            for old_key, new_path in (
                (manifest_payload.get("dxf_path"), input_path),
                (manifest_payload.get("output_dir"), output_dir),
            ):
                if old_key:
                    replacements[str(old_key)] = new_path

            artifact_roles = {
                "numbered_dxf",
                "nested_dxf",
                "report",
                "check",
                "recognition_confirmation",
            }
            artifacts = context_payload.get("artifacts") or {}
            for role in artifact_roles:
                old_path = artifacts.get(role)
                new_path = imported.files.get(role)
                if old_path and new_path:
                    replacements[str(old_path)] = new_path

            confirmed = context_payload.get("confirmed_drawing") or {}
            if (
                confirmed.get("artifact_path")
                and imported.files.get("confirmed_drawing")
            ):
                replacements[str(confirmed["artifact_path"])] = imported.files[
                    "confirmed_drawing"
                ]

            revision_paths = context_payload.get("nesting_revision_paths") or {}
            for revision_id, old_path in revision_paths.items():
                role = (
                    "nesting_revision_"
                    + _safe_id(str(revision_id), "revision")
                )
                new_path = imported.files.get(role)
                if old_path and new_path:
                    replacements[str(old_path)] = new_path

            for role, old_path in (
                (manifest_payload.get("delivery") or {}).get("artifacts") or {}
            ).items():
                capsule_role = (
                    "delivery_" + _safe_id(str(role), "artifact")
                )
                new_path = imported.files.get(capsule_role)
                if old_path and new_path:
                    replacements[str(old_path)] = new_path
            word_report = manifest_payload.get("word_report") or {}
            if word_report.get("path") and imported.files.get("word_report"):
                replacements[str(word_report["path"])] = imported.files[
                    "word_report"
                ]
            for role, old_path in (
                manifest_payload.get("texture_package") or {}
            ).items():
                capsule_role = (
                    "texture_" + _safe_id(str(role), "artifact")
                )
                new_path = imported.files.get(capsule_role)
                if old_path and new_path:
                    replacements[str(old_path)] = new_path

            local_manifest = remap_json_paths(
                manifest_payload,
                replacements,
            )
            local_context = remap_json_paths(
                context_payload,
                replacements,
            )
            local_manifest["job_id"] = job_id
            local_manifest["dxf_path"] = str(input_path)
            local_manifest["output_dir"] = str(output_dir)
            local_manifest["stage"] = session.current_stage
            local_manifest["updated_at"] = _utc_now()
            local_manifest["imported_capsule"] = {
                "capsule_id": info.manifest.get("capsule_id"),
                "capsule_digest": info.manifest.get("capsule_digest"),
                "imported_at": _utc_now(),
            }
            output_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "manifest.json").write_text(
                json.dumps(local_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (job_dir / "context.json").write_text(
                json.dumps(local_context, ensure_ascii=False),
                encoding="utf-8",
            )
            WorkflowSessionStore(output_dir).save(session)
            self._manifests[job_id] = local_manifest
            self._jobs.pop(job_id, None)
            runtime, manifest = self._runtime_and_manifest(job_id)
            state = self._public_state(runtime, manifest)
            state["imported_capsule"] = local_manifest["imported_capsule"]
            return state
        except StonebotProtocolError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted capsule boundary
            shutil.rmtree(job_dir, ignore_errors=True)
            raise StonebotProtocolError(
                "CAPSULE_IMPORT_FAILED",
                f"任务胶囊无法恢复: {exc}",
                allowed_actions=["stonebot_import_task_capsule"],
            ) from exc

    # ------------------------------------------------------------------
    # Runtime construction and configuration
    # ------------------------------------------------------------------

    def _make_runtime(
        self,
        manifest: dict[str, Any],
        *,
        restore_session=None,
    ) -> AgentRuntime:
        initial = manifest.get("initial") or {}
        config = manifest.get("nesting")
        width_mm = float((config or {}).get("width_mm", 3200))
        height_mm = float((config or {}).get("height_mm", 1600))
        thickness_mm = float((config or {}).get("thickness_mm", 20))
        profile = self._build_profile(config)
        sheet_spec = self._build_sheet_spec(config) if config else None
        process_policy = self._build_process_policy(config, profile)
        output_dir = Path(manifest["output_dir"])
        project_id = _safe_id(
            str(manifest.get("project_id") or "project"),
            "project",
        )
        tenant_id = _safe_id(
            str(manifest.get("tenant_id") or "default"),
            "default",
        )
        project_root = self.data_root / "tenants" / tenant_id / project_id
        job_id = str(manifest["job_id"])
        runtime = AgentRuntime(
            str(manifest["dxf_path"]),
            width_mm,
            height_mm,
            thickness_mm,
            UnitSystem.METRIC,
            profile,
            provider_name="queue",
            sheet_spec=sheet_spec,
            process_policy=process_policy,
            trials=int((config or {}).get("trials", 1)),
            seed=int((config or {}).get("seed", 0)),
            budget=float((config or {}).get("budget", 0)),
            quick=bool((config or {}).get("quick", True)),
            certified=bool((config or {}).get("certified", False)),
            pairing=bool((config or {}).get("pairing", False)),
            skip_unnumbered=bool(initial.get("skip_unnumbered", True)),
            beam=bool((config or {}).get("beam", True)),
            beam_width=(config or {}).get("beam_width"),
            compress=bool((config or {}).get("compress", True)),
            rect_fast=bool((config or {}).get("rect_fast", True)),
            low_fill_rework=bool(
                (config or {}).get("low_fill_rework", False)
            ),
            low_fill_threshold=float(
                (config or {}).get("low_fill_threshold", 0.6)
            ),
            candidate_mode=str(
                (config or {}).get("candidate_mode", "bbox")
            ),
            output_dir=output_dir,
            tenant_id=tenant_id,
            project_id=project_id,
            recognition_memory_path=(
                project_root / "recognition_memory.json"
            ),
            recognition_model_path=(
                self.data_root
                / "recognition_models"
                / "current.json"
            ),
            global_feedback_dir=(
                self.data_root
                / "recognition_models"
                / "global_feedback"
            ),
            session_id=job_id,
            restore_session=restore_session,
            use_template=bool(initial.get("use_template", False)),
            cold_start=bool(initial.get("cold_start", False)),
        )
        if restore_session is not None:
            stage = runtime.session.stage
            runtime.last_result = AgentLoopResult(
                session_id=job_id,
                stage=stage.value,
                completed=stage == WorkflowStage.COMPLETED,
                blocked=stage == WorkflowStage.BLOCKED,
                confirmation_required=stage in {
                    WorkflowStage.AUDITED,
                    WorkflowStage.NESTED_REPORTED,
                    WorkflowStage.POSTPROCESS_CONFIRMED,
                },
                tool_results=[],
            )
        return runtime

    def _normalize_nesting_config(
        self,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        config = dict(args)
        config.pop("job_id", None)
        config["width_mm"] = float(config["width_mm"])
        config["height_mm"] = float(config["height_mm"])
        config["thickness_mm"] = float(
            config.get("thickness_mm", 20)
        )
        config["process"] = str(config["process"]).casefold()
        config["kerf_mm"] = float(config.get("kerf_mm", 0))
        if config.get("circular_saw_diameter_mm") is not None:
            config["circular_saw_diameter_mm"] = float(
                config["circular_saw_diameter_mm"]
            )
            config["saw_plunge_extra_mm"] = float(
                config.get("saw_plunge_extra_mm", 2)
            )
            config["saw_safety_margin_mm"] = float(
                config.get("saw_safety_margin_mm", 0)
            )
        config["certified"] = bool(config.get("certified", False))
        if config["width_mm"] <= 0 or config["height_mm"] <= 0:
            raise StonebotProtocolError(
                "INVALID_SHEET_SIZE",
                "大板长宽必须大于 0。",
            )
        if config["process"] not in {"bridge", "waterjet", "laser"}:
            raise StonebotProtocolError(
                "INVALID_PROCESS",
                "process 必须是 bridge、waterjet 或 laser。",
            )
        if config["process"] == "bridge" and config["kerf_mm"] <= 0:
            raise StonebotProtocolError(
                "BRIDGE_KERF_REQUIRED",
                "桥切排板必须提供大于 0 的 kerf_mm。",
            )
        if (
            config["process"] != "bridge"
            and config.get("circular_saw_diameter_mm") is not None
        ):
            raise StonebotProtocolError(
                "SAW_REQUIRES_BRIDGE",
                "圆盘锯让刀参数只适用于桥切（bridge）工艺。",
            )
        if (
            config["process"] == "bridge"
            and config.get("circular_saw_diameter_mm") is not None
        ):
            try:
                CircularSawPolicy(
                    diameter_mm=float(
                        config["circular_saw_diameter_mm"]
                    ),
                    plunge_extra_mm=float(
                        config.get("saw_plunge_extra_mm", 2)
                    ),
                    safety_margin_mm=float(
                        config.get("saw_safety_margin_mm", 0)
                    ),
                ).clearance_for_thickness(config["thickness_mm"])
            except ValueError as exc:
                raise StonebotProtocolError(
                    "INVALID_SAW_GEOMETRY",
                    f"圆盘锯参数无法形成有效让刀宽度：{exc}",
                ) from exc
        if config.get("low_fill_rework") and config["process"] == "bridge":
            raise StonebotProtocolError(
                "LOW_FILL_REQUIRES_DEEPNEST",
                "低填充板重排只适用于水刀或激光。",
            )
        if config.get("candidate_mode", "bbox") == "nfp_lite" and (
            config["process"] == "bridge"
        ):
            raise StonebotProtocolError(
                "NFP_REQUIRES_DEEPNEST",
                "NFP-lite 候选只适用于水刀或激光。",
            )
        if config.get("low_fill_rework") and config.get("pairing"):
            raise StonebotProtocolError(
                "INCOMPATIBLE_OPTIONS",
                "低填充重排不能与 pairing 同时启用。",
            )
        if config["certified"] and config["process"] != "bridge":
            raise StonebotProtocolError(
                "CERTIFIED_REQUIRES_BRIDGE",
                "认证确定性模式当前只支持桥切（bridge）工艺。",
            )
        return config

    def _apply_nesting_config(
        self,
        runtime: AgentRuntime,
        config: dict[str, Any],
    ) -> None:
        profile = self._build_profile(config)
        runtime.context.nesting_profile = profile
        runtime.context.sheet_spec = self._build_sheet_spec(config)
        runtime.context.process_policy = self._build_process_policy(
            config,
            profile,
        )
        runtime.context.width_mm = float(config["width_mm"])
        runtime.context.height_mm = float(config["height_mm"])
        runtime.context.thickness_mm = float(config["thickness_mm"])
        runtime.context.trials = int(config.get("trials", 1))
        runtime.context.seed = int(config.get("seed", 0))
        runtime.context.budget = float(config.get("budget", 0))
        runtime.context.quick = bool(config.get("quick", True))
        runtime.context.certified = bool(config.get("certified", False))
        runtime.context.pairing = bool(config.get("pairing", False))
        runtime.context.beam = bool(config.get("beam", True))
        runtime.context.beam_width = config.get("beam_width")
        runtime.context.compress = bool(config.get("compress", True))
        runtime.context.rect_fast = bool(config.get("rect_fast", True))
        runtime.context.low_fill_rework = bool(
            config.get("low_fill_rework", False)
        )
        runtime.context.low_fill_threshold = float(
            config.get("low_fill_threshold", 0.6)
        )
        runtime.context.candidate_mode = str(
            config.get("candidate_mode", "bbox")
        )
        suffix = (
            f"{int(runtime.context.width_mm)}"
            f"x{int(runtime.context.height_mm)}"
        )
        stem = runtime.dxf_path.stem
        runtime.nested_path = str(
            runtime.output_dir / f"{stem}_nested_{suffix}.dxf"
        )
        runtime.report_path = str(
            runtime.output_dir / f"{stem}_report_{suffix}.json"
        )

    def _build_profile(self, config: dict[str, Any] | None):
        config = config or {}
        profile_name = str(config.get("profile") or "min_sheets")
        if profile_name not in PROFILES:
            raise StonebotProtocolError(
                "UNKNOWN_PROFILE",
                f"未知排板模板: {profile_name}",
            )
        profile = PROFILES[profile_name]
        process = config.get("process")
        overrides: dict[str, Any] = {}
        if process in {"waterjet", "laser"}:
            overrides["processing_class"] = "waterjet_laser"
        elif process == "bridge":
            overrides["processing_class"] = "bridge"
        if "min_gap" in config:
            overrides["min_gap"] = float(config["min_gap"])
        if "rotation_degrees" in config:
            overrides["rotation"] = [
                int(float(value))
                for value in config["rotation_degrees"]
            ]
            overrides["arbitrary_rotation"] = bool(
                config.get("free_rotation", False)
            )
        elif config.get("free_rotation"):
            overrides["arbitrary_rotation"] = True
        if "group_mode" in config:
            overrides["group_mode"] = config["group_mode"]
        if "slide_to_edge" in config:
            overrides["slide_to_edge"] = bool(config["slide_to_edge"])
        if "align_edges" in config:
            overrides["align_edges"] = bool(config["align_edges"])
        if "first_part_left_edge" in config:
            overrides["first_part_left_edge"] = bool(
                config["first_part_left_edge"]
            )
        overrides["sheet_thickness"] = float(
            config.get("thickness_mm", 20)
        )
        return profile.with_overrides(**overrides)

    def _build_sheet_spec(
        self,
        config: dict[str, Any] | None,
    ) -> SheetSpec | None:
        if not config:
            return None
        width = float(config["width_mm"])
        height = float(config["height_mm"])
        return SheetSpec(
            nominal_width_mm=width,
            nominal_height_mm=height,
            usable_width_mm=float(
                config.get("usable_width_mm") or width
            ),
            usable_height_mm=float(
                config.get("usable_height_mm") or height
            ),
            thickness_mm=float(config.get("thickness_mm", 20)),
            oversize_enabled=bool(
                config.get("oversize_enabled", False)
            ),
            oversize_mode=OversizeMode(
                str(config.get("oversize_mode", "total"))
            ),
            oversize_width_mm=float(
                config.get("oversize_width_mm", 0)
            ),
            oversize_height_mm=float(
                config.get("oversize_height_mm", 0)
            ),
        )

    def _build_process_policy(
        self,
        config: dict[str, Any] | None,
        profile,
    ) -> ProcessPolicy | None:
        if not config or not config.get("process"):
            return None
        process = str(config["process"])
        kerf_mm = float(config.get("kerf_mm", 0))
        rotation_degrees = config.get("rotation_degrees")
        allow_shared_edge = bool(
            config.get("allow_shared_edge", True)
        )
        if process == "bridge":
            saw_diameter = config.get("circular_saw_diameter_mm")
            return ProcessPolicy.bridge(
                kerf_mm=kerf_mm,
                min_gap_mm=profile.min_gap,
                rotation_degrees=rotation_degrees,
                allow_shared_edge=allow_shared_edge,
                circular_saw=(
                    CircularSawPolicy(
                        diameter_mm=float(saw_diameter),
                        plunge_extra_mm=float(
                            config.get("saw_plunge_extra_mm", 2)
                        ),
                        safety_margin_mm=float(
                            config.get("saw_safety_margin_mm", 0)
                        ),
                    )
                    if saw_diameter is not None
                    else None
                ),
            )
        if process == "waterjet":
            return ProcessPolicy.waterjet(
                min_gap_mm=profile.min_gap,
                kerf_mm=kerf_mm,
                allow_shared_edge=allow_shared_edge,
            )
        return ProcessPolicy.laser(
            min_gap_mm=profile.min_gap,
            kerf_mm=kerf_mm,
            allow_shared_edge=allow_shared_edge,
        )

    # ------------------------------------------------------------------
    # State, persistence and summaries
    # ------------------------------------------------------------------

    def _runtime_and_manifest(
        self,
        job_id: str,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        runtime = self._jobs.get(job_id)
        manifest = self._manifests.get(job_id)
        if manifest is None:
            manifest = self._load_manifest(job_id)
            self._manifests[job_id] = manifest
        if runtime is None:
            session_path = WorkflowSessionStore(
                Path(manifest["output_dir"])
            ).path_for(job_id)
            if session_path.exists():
                session = WorkflowSessionStore(
                    Path(manifest["output_dir"])
                ).load(job_id)
                runtime = self._make_runtime(
                    manifest,
                    restore_session=session,
                )
                snapshot_path = self._context_path(job_id)
                if not snapshot_path.exists():
                    raise StonebotProtocolError(
                        "JOB_CONTEXT_UNAVAILABLE",
                        "任务会话存在，但几何上下文快照缺失，"
                        "不能安全继续。",
                        details={"job_id": job_id},
                        allowed_actions=[
                            "stonebot_analyze_drawing",
                            "stonebot_job_status",
                        ],
                    )
                try:
                    restore_context_snapshot(
                        runtime.context,
                        snapshot_path,
                    )
                except Exception as exc:  # noqa: BLE001 - persistence boundary
                    raise StonebotProtocolError(
                        "JOB_CONTEXT_INVALID",
                        f"几何上下文快照无法恢复: {exc}",
                        details={"job_id": job_id},
                        allowed_actions=[
                            "stonebot_analyze_drawing",
                            "stonebot_job_status",
                        ],
                    ) from exc
                self._jobs[job_id] = runtime
            else:
                raise StonebotProtocolError(
                    "JOB_CONTEXT_UNAVAILABLE",
                    "任务元数据存在，但当前进程没有可继续的几何上下文。",
                    details={"job_id": job_id},
                    allowed_actions=[
                        "stonebot_analyze_drawing",
                        "stonebot_job_status",
                    ],
                )
        return runtime, manifest

    def _sync_manifest(
        self,
        job_id: str,
        runtime: AgentRuntime,
    ) -> None:
        manifest = self._manifests[job_id]
        public = self._public_state(runtime, manifest)
        save_context_snapshot(
            runtime.context,
            self._context_path(job_id),
        )
        manifest["stage"] = runtime.session.current_stage
        manifest["updated_at"] = _utc_now()
        manifest["last_summary"] = public.get("summary", {})
        self._save_manifest(manifest)

    def _context_path(self, job_id: str) -> Path:
        return (
            self.jobs_root
            / _safe_id(job_id, "invalid")
            / "context.json"
        )

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        path = self.jobs_root / str(manifest["job_id"]) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_manifest(self, job_id: str) -> dict[str, Any]:
        path = self.jobs_root / _safe_id(job_id, "invalid") / "manifest.json"
        if not path.exists():
            raise StonebotProtocolError(
                "JOB_NOT_FOUND",
                f"任务不存在: {job_id}",
                allowed_actions=["stonebot_list_jobs"],
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StonebotProtocolError(
                "JOB_MANIFEST_INVALID",
                f"任务清单损坏: {exc}",
            ) from exc
        return payload

    def _public_state(
        self,
        runtime: AgentRuntime,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        status = runtime.status()
        snapshot = runtime.context.snapshot()
        parts = runtime.context.parts or []
        hole_count = sum(len(part.holes) for part in parts)
        numbered_count = sum(
            1
            for part in parts
            if str(getattr(part, "number", "") or "").strip()
        )
        issue_counts: dict[str, int] = {}
        for issue in runtime.context.audit_issues:
            issue_counts[issue.severity] = (
                issue_counts.get(issue.severity, 0) + 1
            )
        stage = runtime.session.current_stage
        summary = {
            "project_id": runtime.context.project_id,
            "dxf_path": runtime.context.dxf_path,
            "drawing_profile": snapshot.get("profile_name"),
            "drawing_profile_source": snapshot.get("profile_source"),
            "process": (
                getattr(
                    runtime.context.process_policy.process,
                    "value",
                    runtime.context.process_policy.process,
                )
                if runtime.context.process_policy is not None
                else None
            ),
            "sheet": snapshot.get("sheet"),
            "certified": bool(runtime.context.certified),
            "counts": {
                "parts": len(parts),
                "holes": hole_count,
                "numbered_parts": numbered_count,
                "audit_issues": len(runtime.context.audit_issues),
                "audit_issue_severity": issue_counts,
            },
            "nesting": snapshot.get("nesting"),
            "correction_revision": runtime.context.correction_revision,
            "confirmed_drawing": snapshot.get("confirmed_drawing"),
            "nesting_revisions": snapshot.get("nesting_revisions"),
        }
        tool_errors = []
        if runtime.last_result is not None:
            tool_errors = [
                result.error
                for result in runtime.last_result.tool_results
                if not result.ok and result.error
            ]
        public_artifacts: dict[str, Any] = {}
        delivery = manifest.get("delivery") or {}
        if stage == WorkflowStage.COMPLETED.value:
            public_artifacts.update(
                delivery.get("artifacts") or {}
            )
            word_report = manifest.get("word_report") or {}
            if (
                word_report.get("path")
                and Path(str(word_report["path"])).is_file()
            ):
                public_artifacts["word_report"] = word_report["path"]
            texture_package = manifest.get("texture_package") or {}
            if (
                texture_package.get("textured_dxf")
                and Path(str(texture_package["textured_dxf"])).is_file()
            ):
                public_artifacts["textured_dxf"] = (
                    texture_package["textured_dxf"]
                )
            if (
                texture_package.get("manifest")
                and Path(str(texture_package["manifest"])).is_file()
            ):
                public_artifacts["texture_manifest"] = (
                    texture_package["manifest"]
                )
            if (
                texture_package.get("archive")
                and Path(str(texture_package["archive"])).is_file()
            ):
                public_artifacts["texture_archive"] = (
                    texture_package["archive"]
                )
        return {
            "ok": (
                runtime.last_error is None
                and not status.get("blocked", False)
            ),
            "schema_version": SCHEMA_VERSION,
            "job_id": runtime.session.session_id,
            "stage": stage,
            "completed": status.get("completed", False),
            "blocked": status.get("blocked", False),
            "confirmation_required": status.get(
                "confirmation_required",
                False,
            ),
            "nesting_configured": bool(
                manifest.get("nesting_configured")
            ),
            "license": (
                self.license_guard.status().to_dict()
                if self.license_guard is not None
                else None
            ),
            "summary": summary,
            "validation_errors": list(runtime.context.validation_errors),
            "errors": tool_errors,
            "error": runtime.last_error,
            "artifacts": public_artifacts,
            "session_artifacts": public_artifacts,
            "next_actions": next_actions(
                stage,
                nesting_configured=bool(
                    manifest.get("nesting_configured")
                ),
            ),
        }

    def _confirm(self, runtime: AgentRuntime, decision: str) -> None:
        normalized = decision.strip().casefold()
        if normalized not in {"confirm", "cancel"}:
            raise StonebotProtocolError(
                "INVALID_DECISION",
                "decision 必须是 confirm 或 cancel。",
            )
        try:
            runtime.confirm(normalized)
        except Exception as exc:  # noqa: BLE001 - service boundary
            raise StonebotProtocolError(
                "CONFIRMATION_FAILED",
                str(exc),
                details={"stage": runtime.session.current_stage},
                allowed_actions=["stonebot_job_status"],
            ) from exc

    def _require_stage(
        self,
        runtime: AgentRuntime,
        expected: set[WorkflowStage],
    ) -> None:
        if runtime.session.stage in expected:
            return
        expected_values = sorted(stage.value for stage in expected)
        raise StonebotProtocolError(
            "INVALID_JOB_STAGE",
            f"当前阶段 {runtime.session.current_stage} 不允许该操作；"
            f"需要 {expected_values}。",
            details={"stage": runtime.session.current_stage},
            allowed_actions=["stonebot_job_status"],
        )

    def _require_job_id(self, args: dict[str, Any]) -> str:
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise StonebotProtocolError(
                "JOB_ID_REQUIRED",
                "缺少 job_id。",
                allowed_actions=["stonebot_list_jobs"],
            )
        return job_id
