import json

import ezdxf
from shapely.geometry import Polygon

import main as app
from src.constraints import PROFILE_QUICK
from src.drawing_profile import DrawingProfile
from src.job_context import JobContext
from src.models import Part
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
    assert isinstance(audit["issues"], int)
    assert assigned["parts"] == 2
    assert numbered["parts"] == 2
    assert numbered_path.exists()


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
