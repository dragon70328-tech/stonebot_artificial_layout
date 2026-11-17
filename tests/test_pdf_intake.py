from __future__ import annotations

import math

import ezdxf
import pymupdf

from src.pdf_intake import (
    PageMap,
    analyze_pdf,
    convert_model_to_dxf,
    inspect_pdf,
    validate_converted_dxf,
)
from src.pdf_intake.dxf_conversion import _circumcircle


def _make_four_board_pdf(path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y0, y1 = 100.0, 700.0
    xs = [100.0, 180.0, 260.0, 340.0, 420.0]
    for x in xs:
        page.draw_line((x, y0), (x, y1), color=(0, 0, 0), width=0.72)
    page.draw_line((xs[0], y0), (xs[-1], y0), color=(0, 0, 0), width=0.72)
    page.draw_line((xs[0], y1), (xs[-1], y1), color=(0, 0, 0), width=0.72)
    for index in range(4):
        center_x = (xs[index] + xs[index + 1]) / 2.0
        page.insert_text(
            (center_x - 3, 295),
            str(index + 1),
            fontsize=7,
            color=(0, 0, 0),
        )
        page.insert_text(
            (center_x - 24, 307),
            "(1197X1197)",
            fontsize=6,
            color=(0, 0, 0),
        )
    doc.save(path)
    doc.close()


def test_preflight_vector_pdf(tmp_path):
    source = tmp_path / "vector.pdf"
    _make_four_board_pdf(source)
    report = inspect_pdf(source)
    assert report.page_count == 1
    assert report.vector_pages == 1
    assert report.is_vector
    assert len(report.sha256) == 64


def test_convert_vector_pdf_to_single_layout(tmp_path):
    source = tmp_path / "vector.pdf"
    output = tmp_path / "inplace.dxf"
    _make_four_board_pdf(source)
    model = analyze_pdf(source, [PageMap(1, "D1F", "D1F")])
    assert len(model.pages[0].cells) == 4
    cells = model.pages[0].cells
    for cell in cells:
        assert abs(
            (cell.rect_pt[2] - cell.rect_pt[0])
            * model.pages[0].mm_per_pt
            - 1197.0
        ) < 1e-6
    assert abs(cells[1].rect_pt[0] - cells[0].rect_pt[2]) < 1e-6

    audit = convert_model_to_dxf(model, output)
    assert audit.layout_count == 1
    assert audit.floor_block_count == 1
    assert audit.finished_part_count == 4
    assert audit.r450_boundary_count == 0
    assert validate_converted_dxf(
        output,
        expected_pages=1,
        expected_board_labels=4,
        expected_r450=0,
    ) == []

    doc = ezdxf.readfile(output)
    assert "D1F" in doc.layouts.names()
    assert len(list(doc.groups)) == 4
    left = [
        entity
        for entity in doc.groups.get("D1F-1")
        if entity.dxftype() == "LWPOLYLINE"
    ][0]
    right = [
        entity
        for entity in doc.groups.get("D1F-2")
        if entity.dxftype() == "LWPOLYLINE"
    ][0]
    assert max(point[0] for point in left.get_points()) == min(
        point[0] for point in right.get_points()
    )


def test_circumcircle_and_bulge():
    arc = _circumcircle((0.0, 0.0), (10.0, 10.0), (20.0, 0.0))
    assert math.isclose(arc.radius, 10.0, abs_tol=1e-9)
    assert math.isclose(arc.center[0], 10.0, abs_tol=1e-9)
    assert math.isclose(arc.center[1], 0.0, abs_tol=1e-9)
    assert math.isclose(arc.bulge((0.0, 0.0), (20.0, 0.0)), -1.0)
    assert math.isclose(arc.bulge((20.0, 0.0), (0.0, 0.0)), 1.0)
