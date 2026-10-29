import json
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest
from shapely.geometry import Polygon

import main as app
from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_QUICK
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

    nested = handlers["nest_parts"](
        {"sheet_width": 1000, "sheet_height": 800, "budget": 0, "quick": True}
    )
    post = handlers["postprocess"]({"result": {}})
    dxf_path = tmp_path / "nested.dxf"
    report_path = tmp_path / "report.json"
    written = handlers["write_dxf"]({"path": str(dxf_path)})
    reported = handlers["write_report"]({"path": str(report_path)})

    assert nested["total_parts"] == 2
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
    with_spec = build_tool_handlers(context)["nest_parts"](
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
