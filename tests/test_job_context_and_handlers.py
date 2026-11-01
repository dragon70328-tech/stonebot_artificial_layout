import json
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest
from shapely.geometry import Polygon

import main as app
from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_QUICK
from src.contracts import ConfirmedDrawing
from src.drawing_profile import DrawingProfile
from src.job_context import JobContext
from src.models import NestingResult, Part, Sheet
from src.nesting_contracts import ProcessPolicy, SheetSpec
from src.tool_handlers import build_tool_handlers
from src.units import UnitSystem


def _make_dxf(path):
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (400, 0), (400, 300), (0, 300), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    msp.add_lwpolyline(
        [(500, 0), (900, 0), (900, 300), (500, 300), (500, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((200, 150))
    text = msp.add_text("01B-2", dxfattribs={"layer": "NUM"})
    text.set_placement((700, 150))
    doc.saveas(path)


def _make_unprofiled_label_rich_dxf(path):
    doc = ezdxf.new("R2010")
    doc.layers.add("VC - 预算组")
    doc.layers.add("FF-TEXT-01")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 600), (0, 600), (0, 0)],
        close=True,
        dxfattribs={"layer": "VC - 预算组"},
    )
    msp.add_lwpolyline(
        [(1200, 0), (1800, 0), (1800, 600), (1200, 600), (1200, 0)],
        close=True,
        dxfattribs={"layer": "VC - 预算组"},
    )
    for x in (30000, 30500, 31000, 31500):
        text = msp.add_text("A-1", dxfattribs={"layer": "FF-TEXT-01"})
        text.set_placement((x, 30000))
    doc.saveas(path)


def _profile():
    return DrawingProfile(
        name="handler_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )


def test_job_context_snapshot_is_json_safe():
    context = JobContext(
        dxf_path="a.dxf",
        project_id="a",
        width_mm=1000,
        height_mm=800,
    )

    payload = json.dumps(context.snapshot(), ensure_ascii=False)

    assert "a.dxf" in payload
    assert '"parts": 0' in payload


def test_job_context_snapshot_includes_effective_sheet_and_process():
    context = JobContext(
        sheet_spec=SheetSpec(
            nominal_width_mm=3200,
            nominal_height_mm=1800,
            usable_width_mm=3200,
            usable_height_mm=1800,
            oversize_enabled=True,
            oversize_mode="per_side",
            oversize_width_mm=30,
            oversize_height_mm=20,
        ),
        process_policy=ProcessPolicy.bridge(kerf_mm=5),
    )

    snapshot = context.snapshot()

    assert snapshot["sheet"]["effective_width_mm"] == 3260
    assert snapshot["sheet"]["effective_height_mm"] == 1840
    assert snapshot["process_policy"]["kerf_mm"] == 5
    json.dumps(snapshot, ensure_ascii=False)


def test_real_handlers_numbering_confirmation_chain(tmp_path):
    dxf_path = tmp_path / "parts.dxf"
    _make_dxf(dxf_path)
    context = JobContext(
        dxf_path=str(dxf_path),
        nesting_profile=app.PROFILES["quick"],
    )
    context.drawing_profile = _profile()
    handlers = build_tool_handlers(context)

    analyzed = handlers["analyze_drawing"]({"path": str(dxf_path)})
    read = handlers["read_dxf"]({"path": str(dxf_path)})
    audit = handlers["audit_drawing"]({"path": str(dxf_path)})
    assigned = handlers["assign_numbers"](
        {"path": str(dxf_path), "skip_unnumbered": False}
    )
    numbered_path = tmp_path / "numbered.dxf"
    numbered = handlers["write_numbered_dxf"]({"path": str(numbered_path)})

    assert analyzed["entity_counts"]["LWPOLYLINE"] == 2
    assert read["parts"] == 2
    assert len(context.closed_entities) == 2
    assert isinstance(audit["issues"], int)
    assert assigned["parts"] == 2
    assert numbered["parts"] == 2
    assert numbered_path.exists()


def test_real_handlers_block_automatic_numbering_without_profile(tmp_path):
    dxf_path = tmp_path / "label-rich-unprofiled.dxf"
    _make_unprofiled_label_rich_dxf(dxf_path)
    context = JobContext(
        dxf_path=str(dxf_path),
        nesting_profile=app.PROFILES["quick"],
        skip_unnumbered=False,
    )
    handlers = build_tool_handlers(context)

    handlers["analyze_drawing"]({"path": str(dxf_path)})
    handlers["read_dxf"]({"path": str(dxf_path)})

    with pytest.raises(ValueError, match="automatic numbering blocked"):
        handlers["assign_numbers"](
            {"path": str(dxf_path), "skip_unnumbered": False}
        )
    assert not context.parts


def test_real_handlers_report_zero_matched_parts_before_dxf_write(tmp_path):
    dxf_path = tmp_path / "label-rich-unprofiled-default.dxf"
    _make_unprofiled_label_rich_dxf(dxf_path)
    context = JobContext(
        dxf_path=str(dxf_path),
        nesting_profile=app.PROFILES["quick"],
    )
    handlers = build_tool_handlers(context)

    handlers["analyze_drawing"]({"path": str(dxf_path)})
    handlers["read_dxf"]({"path": str(dxf_path)})

    with pytest.raises(ValueError, match="numbering blocked: 0 panels"):
        handlers["assign_numbers"]({"path": str(dxf_path)})
    assert not context.parts


def test_real_handlers_nest_postprocess_write_chain(tmp_path):
    parts = [
        Part(
            id=1,
            number="P-0001",
            polygon=Polygon([(0, 0), (400, 0), (400, 300), (0, 300)]),
            outer_polygon=Polygon([(0, 0), (400, 0), (400, 300), (0, 300)]),
        ),
        Part(
            id=2,
            number="P-0002",
            polygon=Polygon([(0, 0), (400, 0), (400, 300), (0, 300)]),
            outer_polygon=Polygon([(0, 0), (400, 0), (400, 300), (0, 300)]),
        ),
    ]
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
    )
    handlers = build_tool_handlers(context)

    confirmed = handlers["confirm_numbering"]({"decision": "confirmed"})
    nested = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    post = handlers["postprocess"]({"result": {}})
    dxf_path = tmp_path / "nested.dxf"
    report_path = tmp_path / "report.json"
    written = handlers["write_dxf"]({"path": str(dxf_path)})
    reported = handlers["write_report"]({"path": str(report_path)})

    assert nested["total_parts"] == 2
    assert confirmed["confirmed_drawing"]["panels"] == 2
    assert post["geometry_errors"] == 0
    assert written["sheets"] == 1
    assert reported["total_sheets"] == 1
    assert dxf_path.exists()
    assert report_path.exists()


def _rect_part(part_id: int, width: float, height: float,
               x: float = 0.0, y: float = 0.0) -> Part:
    polygon = Polygon([
        (x, y), (x + width, y), (x + width, y + height), (x, y + height),
    ])
    return Part(
        id=part_id,
        number=f"P-{part_id:04d}",
        polygon=polygon,
        outer_polygon=polygon,
    )


def _four_panel_result() -> NestingResult:
    """4 件 500x400 铺满 1000x800：纵向 1 刀、横向 1 刀。"""
    parts = [
        _rect_part(1, 500, 400, 0, 0),
        _rect_part(2, 500, 400, 500, 0),
        _rect_part(3, 500, 400, 0, 400),
        _rect_part(4, 500, 400, 500, 400),
    ]
    sheet = Sheet(index=1, width=1000, height=800, thickness=20, parts=parts)
    return NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=len(parts),
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=sheet.total_area,
    )


def _bridge_spec(oversize_total: float) -> SheetSpec:
    return SheetSpec(
        nominal_width_mm=1000,
        nominal_height_mm=800,
        usable_width_mm=1000,
        usable_height_mm=800,
        oversize_enabled=oversize_total > 0,
        oversize_mode="total",
        oversize_width_mm=oversize_total,
        oversize_height_mm=oversize_total,
    )


def test_handlers_nest_uses_contract_usable_size(tmp_path):
    """排板空间取契约的可利用尺寸，而不是工具参数/标称尺寸。"""
    parts = [_rect_part(1, 700, 500), _rect_part(2, 700, 500)]
    handlers = build_tool_handlers(JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
    ))
    handlers["confirm_numbering"]({"decision": "confirmed"})
    without_spec = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )

    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
        sheet_spec=SheetSpec(
            nominal_width_mm=1000,
            nominal_height_mm=800,
            usable_width_mm=700,
            usable_height_mm=800,
            thickness_mm=20,
        ),
    )
    handlers_with_spec = build_tool_handlers(context)
    handlers_with_spec["confirm_numbering"]({"decision": "confirmed"})
    with_spec = handlers_with_spec["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )

    # 无契约：按工具参数 1000x800 排，两件叠放可用 1 张
    assert without_spec["total_sheets"] == 1
    # 有契约：可利用只有 700x800，两件 700x500 放不下同一张
    assert (context.width_mm, context.height_mm) == (700.0, 800.0)
    assert with_spec["total_sheets"] == 2


def test_handlers_block_when_kerf_material_exceeds_usable_plus_oversize(tmp_path):
    """桥切刀缝材料校验接进 API/agent 路径：没有让尺就拒绝出图。"""
    tight = JobContext(
        nesting_profile=PROFILE_QUICK,
        result=_four_panel_result(),
        unit=UnitSystem.METRIC,
        sheet_spec=_bridge_spec(0.0),
        process_policy=ProcessPolicy.bridge(kerf_mm=10),
    )
    tight_handlers = build_tool_handlers(tight)
    blocked = tight_handlers["postprocess"]({"result": {}})

    # 1000x800 满排 + 纵/横各 1 刀 = 需要 1010x810 > 允许 1000x800
    # （每张板合并成一条错误，X/Y 都写在里面）
    assert blocked["kerf_material_errors"] == 1
    kerf_error = next(
        error for error in tight.validation_errors if "刀路材料超限" in error
    )
    assert "1010.0" in kerf_error
    assert "810.0" in kerf_error
    with pytest.raises(ValueError, match="validation failed"):
        tight_handlers["write_dxf"]({"path": str(tmp_path / "blocked.dxf")})
    assert not (tmp_path / "blocked.dxf").exists()

    # 声明让尺 30：允许 = 实物 1030x830，1010x810 通过
    loose = JobContext(
        nesting_profile=PROFILE_QUICK,
        result=_four_panel_result(),
        unit=UnitSystem.METRIC,
        sheet_spec=_bridge_spec(30.0),
        process_policy=ProcessPolicy.bridge(kerf_mm=10),
    )
    loose_handlers = build_tool_handlers(loose)
    passed = loose_handlers["postprocess"]({"result": {}})

    assert passed["kerf_material_errors"] == 0
    assert loose.validation_errors == []
    assert loose.kerf_metrics["vertical_cuts"] == 1
    assert loose.kerf_metrics["horizontal_cuts"] == 1
    written = loose_handlers["write_dxf"]({"path": str(tmp_path / "ok.dxf")})
    assert written["sheets"] == 1

    # 报告里必须能还原口径：标称 / 让尺 / 可利用 / 实物 / 刀缝允许
    loose.nesting_outcome = SimpleNamespace(
        total_min_sheets=1,
        total_special_pair_min_sheets=None,
        material_summary=[],
        elapsed_seconds=0.0,
    )
    report_path = tmp_path / "report.json"
    loose_handlers["write_report"]({"path": str(report_path)})
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dimensions = report["sheet_dimensions"]

    assert dimensions["width"] == 1000
    assert dimensions["usable_width"] == 1000
    assert dimensions["sheet_oversize_x"] == 30
    assert dimensions["physical_width"] == 1030
    assert dimensions["kerf_allowance_x_mm"] == 30
    assert dimensions["usable_includes_oversize"] is False
    assert report["kerf_material"]["vertical_cuts"] == 1


def test_handlers_waterjet_skips_kerf_material_check(tmp_path):
    """水刀/激光刀缝按口径"可不计"，不参与刀缝材料校验。"""
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        result=_four_panel_result(),
        unit=UnitSystem.METRIC,
        sheet_spec=_bridge_spec(0.0),
        process_policy=ProcessPolicy.waterjet(kerf_mm=10),
    )
    handlers = build_tool_handlers(context)

    result = handlers["postprocess"]({"result": {}})

    assert result["kerf_material_errors"] == 0
    assert context.validation_errors == []
    assert context.kerf_metrics is None


def test_agent_runtime_job_reports_board_caliber(tmp_path):
    """端到端（真实 DXF + 真实工具链）：契约口径必须进报告。

    可利用尺寸小于标称时，刀缝校验允许量 = 实物 − 可利用（而不是只算让尺）。
    """
    dxf_path = tmp_path / "runtime_caliber.dxf"
    _make_dxf(dxf_path)

    runtime = AgentRuntime(
        str(dxf_path),
        1000,
        800,
        20,
        UnitSystem.METRIC,
        PROFILE_QUICK,
        provider_name="queue",
        sheet_spec=SheetSpec(
            nominal_width_mm=1000,
            nominal_height_mm=800,
            usable_width_mm=700,
            usable_height_mm=800,
            oversize_enabled=True,
            oversize_mode="total",
            oversize_width_mm=30,
            oversize_height_mm=30,
        ),
        process_policy=ProcessPolicy.bridge(kerf_mm=5),
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        skip_unnumbered=False,
        output_dir=tmp_path,
        session_id="runtime-caliber",
    )

    runtime.run_next()
    runtime.confirm("yes")
    runtime.confirm("confirm")
    runtime.confirm("y")

    assert runtime.status()["completed"] is True
    # 排板空间 = 契约的可利用尺寸，不是标称 1000x800
    assert (runtime.context.width_mm, runtime.context.height_mm) == (700.0, 800.0)

    report = json.loads(Path(runtime.report_path).read_text(encoding="utf-8"))
    dimensions = report["sheet_dimensions"]

    assert dimensions["width"] == 1000          # 标称
    assert dimensions["usable_width"] == 700    # 可利用
    assert dimensions["sheet_oversize_x"] == 30  # 让尺（标称之外的实物余量）
    assert dimensions["physical_width"] == 1030  # 实物 = 标称 + 让尺
    assert dimensions["kerf_allowance_x_mm"] == 330  # 实物 − 可利用
    assert dimensions["usable_includes_oversize"] is False


def test_confirm_numbering_materializes_immutable_snapshot(tmp_path):
    """确认编号产出不可变 ConfirmedDrawing：内容寻址、可回读、进状态。"""
    dxf_path = tmp_path / "parts.dxf"
    _make_dxf(dxf_path)
    context = JobContext(
        dxf_path=str(dxf_path),
        nesting_profile=app.PROFILES["quick"],
        output_dir=tmp_path / "out",
    )
    context.drawing_profile = _profile()
    handlers = build_tool_handlers(context)
    handlers["analyze_drawing"]({"path": str(dxf_path)})
    handlers["read_dxf"]({"path": str(dxf_path)})
    handlers["assign_numbers"](
        {"path": str(dxf_path), "skip_unnumbered": False}
    )

    confirmed = handlers["confirm_numbering"]({"decision": "confirmed"})

    snapshot = confirmed["confirmed_drawing"]
    assert snapshot["panels"] == 2
    assert snapshot["revision_no"] == 1
    path = Path(snapshot["path"])
    assert path.exists()

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["confirmed_digest"] == snapshot["digest"]
    assert stored["confirmed_id"].startswith("cd-")
    assert stored["source_digest"] is not None

    restored = ConfirmedDrawing.model_validate(stored)
    assert len(restored.panels) == 2
    numbers = {panel.number for panel in restored.panels}
    assert numbers == {"01B-1", "01B-2"}

    state = context.snapshot()
    assert state["confirmed_drawing"]["digest"] == snapshot["digest"]
    json.dumps(state, ensure_ascii=False)


def test_confirm_numbering_reject_leaves_no_snapshot():
    """拒绝确认不产生快照，排板仍然被拒绝。"""
    parts = [_rect_part(1, 400, 300)]
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
    )
    handlers = build_tool_handlers(context)

    result = handlers["confirm_numbering"]({"decision": "reject"})

    assert "confirmed_drawing" not in result
    assert context.confirmed_drawing is None
    with pytest.raises(ValueError, match="confirm_numbering"):
        handlers["nest_parts"](
            {"sheet_width": 1000, "sheet_height": 800, "budget": 0}
        )


def test_confirmed_drawing_signature_stable_and_versioned():
    """同一批零件重复确认：内容签名不变，版本链递增并记录父摘要。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
    )
    handlers = build_tool_handlers(context)

    first = handlers["confirm_numbering"]({"decision": "confirmed"})
    first = first["confirmed_drawing"]
    first_signature = context.confirmed_parts_signature

    second = handlers["confirm_numbering"]({"decision": "confirmed"})
    second = second["confirmed_drawing"]

    assert second["revision_no"] == 2
    assert context.confirmed_parts_signature == first_signature
    assert context.confirmed_drawing is not None
    parent_note = f"parent_confirmed_digest:{first['digest']}"
    assert parent_note in context.confirmed_drawing.notes


def test_nest_parts_blocked_when_parts_drift_after_confirmation():
    """确认后零件被改动 => 排板 fail closed；重新确认后放行。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
    )
    handlers = build_tool_handlers(context)
    handlers["confirm_numbering"]({"decision": "confirmed"})

    context.parts = parts + [_rect_part(3, 400, 300)]
    with pytest.raises(ValueError, match="re-run confirm_numbering"):
        handlers["nest_parts"](
            {"sheet_width": 1000, "sheet_height": 800, "budget": 0}
        )

    reconfirmed = handlers["confirm_numbering"]({"decision": "confirmed"})
    assert reconfirmed["confirmed_drawing"]["revision_no"] == 2
    assert reconfirmed["confirmed_drawing"]["panels"] == 3
    nested = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    assert nested["total_parts"] == 3


def _confirmed_context(parts, **kwargs):
    context = JobContext(
        nesting_profile=PROFILE_QUICK,
        parts=parts,
        unit=UnitSystem.METRIC,
        **kwargs,
    )
    handlers = build_tool_handlers(context)
    handlers["confirm_numbering"]({"decision": "confirmed"})
    return context, handlers


def test_nest_parts_records_immutable_revision(tmp_path):
    """每次排板产出不可变版本：请求快照 + 布局 + 指标，内容寻址落盘。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context, handlers = _confirmed_context(
        parts, output_dir=tmp_path / "out"
    )

    nested = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )

    revision_info = nested["revision"]
    assert revision_info["revision_id"].startswith("nr-")
    assert revision_info["request_id"].startswith("nreq-")
    assert len(context.nesting_revisions) == 1
    assert context.active_revision_id == revision_info["revision_id"]

    revision = context.nesting_revisions[0]
    assert revision.request.confirmed_drawing is context.confirmed_drawing
    assert revision.metrics["total_sheets"] == 1.0
    assert len(revision.layout["sheets"]) == 1
    assert len(revision.layout["sheets"][0]["parts"]) == 2

    path = Path(revision_info["path"])
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["revision_id"] == revision_info["revision_id"]
    assert stored["request"]["confirmed_drawing"]["confirmed_digest"]

    state = context.snapshot()
    assert state["nesting_revisions"]["count"] == 1
    assert state["nesting_revisions"]["active"] == revision_info["revision_id"]
    json.dumps(state, ensure_ascii=False)


def test_rollback_nesting_restores_previous_layout_byte_identical(tmp_path):
    """验收口径：重排后回滚到 r1，交付 DXF 与 r1 首次交付逐字节一致。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context, handlers = _confirmed_context(parts)

    first = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    first_revision = first["revision"]["revision_id"]
    assert first["total_sheets"] == 1
    # 新版本未经后处理校验，出图被拒绝
    with pytest.raises(ValueError, match="postprocess on the active"):
        handlers["write_dxf"]({"path": str(tmp_path / "too_early.dxf")})
    handlers["postprocess"]({"result": {}})
    first_dxf = tmp_path / "first.dxf"
    handlers["write_dxf"]({"path": str(first_dxf)})
    first_bytes = first_dxf.read_bytes()

    second = handlers["nest_parts"](
        {"sheet_width": 450, "sheet_height": 350, "budget": 0, "quick": True}
    )
    assert second["total_sheets"] == 2
    assert second["revision"]["revision_id"] != first_revision
    assert len(context.nesting_revisions) == 2
    handlers["postprocess"]({"result": {}})

    rolled_back = handlers["rollback_nesting"](
        {"revision_id": first_revision}
    )
    assert rolled_back["total_sheets"] == 1
    # r1 已携带校验状态，回滚后可直接交付
    assert rolled_back["requires_postprocess"] is False
    assert context.active_revision_id == first_revision
    assert len(context.nesting_revisions) == 2  # 历史不被覆盖
    restored_dxf = tmp_path / "restored.dxf"
    handlers["write_dxf"]({"path": str(restored_dxf)})
    assert restored_dxf.read_bytes() == first_bytes


def test_rollback_nesting_unknown_revision_raises():
    parts = [_rect_part(1, 400, 300)]
    _context, handlers = _confirmed_context(parts)
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    with pytest.raises(ValueError, match="unknown nesting revision"):
        handlers["rollback_nesting"]({"revision_id": "nr-does-not-exist"})


def test_rollback_rejects_revision_from_old_confirmed_drawing():
    """图纸修正后不能再回滚到修正前的布局（否则交付与图纸不符）。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context, handlers = _confirmed_context(parts)
    old = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    old_revision = old["revision"]["revision_id"]
    old_digest = context.confirmed_drawing_digest

    # 修正图纸：换掉零件后重新确认，生成新的确认快照
    context.parts.append(_rect_part(3, 300, 200))
    handlers["confirm_numbering"]({"decision": "confirmed"})
    assert context.confirmed_drawing_digest != old_digest
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )

    with pytest.raises(ValueError, match="different confirmed drawing"):
        handlers["rollback_nesting"]({"revision_id": old_revision})


def test_full_validation_catches_dropped_part():
    """统一验证器：排板结果少一件 => 完整性校验拦截。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context, handlers = _confirmed_context(parts)
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    # 模拟引擎丢件：直接从结果里拿掉一件
    context.result.sheets[0].parts.pop()
    context.result.total_parts -= 1

    result = handlers["postprocess"]({"result": {}})

    assert result["completeness_errors"] == 1
    assert context.validation_errors


def test_full_validation_catches_duplicate_numbers():
    """统一验证器：同一编号出现在两张板 => 唯一性校验拦截。"""
    parts = [_rect_part(1, 400, 300), _rect_part(2, 400, 300)]
    context, handlers = _confirmed_context(parts)
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    # 板上两个零件改成同一编号
    for part in context.result.sheets[0].parts:
        part.number = "P-0001"

    result = handlers["postprocess"]({"result": {}})

    assert result["validation_categories"]["uniqueness"] == 1


def test_write_dxf_readback_gate_blocks_tampered_file(tmp_path):
    """回读门禁：读回校验失败时删除文件并拒绝交付。"""
    parts = [_rect_part(1, 400, 300)]
    context, handlers = _confirmed_context(parts)
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    handlers["postprocess"]({"result": {}})
    dxf_path = tmp_path / "nested.dxf"
    handlers["write_dxf"]({"path": str(dxf_path)})
    assert dxf_path.exists()

    # 正常写出的文件能通过回读；往 OUTER 层塞一个多余轮廓则必须失败
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    doc.modelspace().add_lwpolyline(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        dxfattribs={"layer": "OUTER"},
    )
    doc.saveas(str(dxf_path))

    from src.nesting_readback import sheets_payload_from_result
    from src.nesting_readback import verify_nested_dxf

    errors = verify_nested_dxf(
        dxf_path, sheets_payload_from_result(context.result)
    )
    assert any("外轮廓不一致" in error for error in errors)


def test_write_dxf_readback_failure_removes_file(tmp_path, monkeypatch):
    """回读失败 => 文件被删除、任务不落 nested_dxf_path。"""
    parts = [_rect_part(1, 400, 300)]
    context, handlers = _confirmed_context(parts)
    handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    handlers["postprocess"]({"result": {}})

    import src.tool_handlers as tool_handlers_module

    monkeypatch.setattr(
        tool_handlers_module,
        "verify_nested_dxf",
        lambda path, sheets: ["readback: 模拟校验失败"],
    )
    dxf_path = tmp_path / "nested.dxf"
    with pytest.raises(ValueError, match="read-back verification failed"):
        handlers["write_dxf"]({"path": str(dxf_path)})
    assert not dxf_path.exists()
    assert context.nested_dxf_path is None
