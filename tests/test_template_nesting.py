from pathlib import Path

import ezdxf
from shapely.geometry import Polygon

from src.models import Part
from src.template_nesting import TemplateRecord, match_template, nest_from_template
from src.workflow import validate_mixed_nesting


def test_match_template_returns_none_for_missing_file(tmp_path):
    assert match_template(tmp_path / "missing.dxf", 3200, 1800) is None


def test_nest_from_template_places_known_part(tmp_path):
    template_dxf = tmp_path / "template.dxf"
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
    )
    msp.add_lwpolyline(
        [(10, 10), (40, 10), (40, 30), (10, 30), (10, 10)]
    )
    msp.add_text("P-1", dxfattribs={"height": 2.0}).set_placement((20, 20))
    doc.saveas(template_dxf)

    record = TemplateRecord(
        name="tiny",
        source_sha256="unused",
        sheet_width=100,
        sheet_height=50,
        thickness=20,
        template_dxf=template_dxf,
    )
    part = Part(
        id=1,
        number="P-1",
        polygon=Polygon([(0, 0), (30, 0), (30, 20), (0, 20)]),
        outer_polygon=Polygon([(0, 0), (30, 0), (30, 20), (0, 20)]),
    )

    result = nest_from_template([part], record)

    assert result.total_sheets == 1
    assert result.total_parts == 1
    assert validate_mixed_nesting(result, min_gap=0.0) == []
