from pathlib import Path
from types import SimpleNamespace

import ezdxf

from src.visual_renderer import write_dxf_overview_svg


def _write_source_dxf(path: Path) -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 50))
    doc.saveas(path)


def test_write_dxf_overview_svg(tmp_path):
    source = tmp_path / "source.dxf"
    _write_source_dxf(source)
    output = tmp_path / "overview.svg"

    result = write_dxf_overview_svg(source, output)

    assert result == output
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "<svg" in text
    assert "<polygon" in text
    assert "01B-1" in text


def test_write_dxf_overview_svg_can_hide_text(tmp_path):
    source = tmp_path / "source.dxf"
    _write_source_dxf(source)
    output = tmp_path / "overview_no_text.svg"

    write_dxf_overview_svg(source, output, include_text=False)

    text = output.read_text(encoding="utf-8")
    assert "01B-1" not in text


def test_write_dxf_overview_svg_marks_issues(tmp_path):
    source = tmp_path / "source.dxf"
    _write_source_dxf(source)
    output = tmp_path / "overview_issues.svg"
    issue = SimpleNamespace(
        issue_id="1",
        issue_type="unclosed_geometry",
        coordinates=(50.0, 50.0),
    )

    write_dxf_overview_svg(source, output, issues=[issue])

    text = output.read_text(encoding="utf-8")
    assert "#1" in text
    assert "unclosed_geometry" in text
