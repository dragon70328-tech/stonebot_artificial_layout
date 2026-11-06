from pathlib import Path

from shapely.geometry import Polygon, box

from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.saw_audit import audit_nested_saw_clearance


def _part(part_id: int, number: str, polygon: Polygon) -> Part:
    return Part(
        id=part_id,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        area=polygon.area,
        label_position=(
            polygon.centroid.x,
            polygon.centroid.y,
        ),
    )


def _l_shape() -> Polygon:
    return Polygon([
        (0, 0),
        (400, 0),
        (400, 100),
        (100, 100),
        (100, 400),
        (0, 400),
    ])


def _write(path: Path, parts: list[Part]) -> None:
    sheet = Sheet(
        index=1,
        width=1000,
        height=600,
        thickness=20,
        parts=parts,
    )
    result = NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=len(parts),
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=600000,
    )
    write_nested_dxf(result, str(path))


def test_audit_reports_neighbour_in_internal_corner(tmp_path: Path):
    path = tmp_path / "risk.dxf"
    _write(
        path,
        [
            _part(1, "L", _l_shape()),
            _part(2, "BLOCK", box(120, 120, 150, 150)),
        ],
    )

    report = audit_nested_saw_clearance(
        path,
        diameter_mm=350,
        thickness_mm=20,
    )

    assert report["safe"] is False
    assert report["points_insufficient"] >= 1
    assert report["issue_pairs"][0]["sheet"] == 1


def test_audit_passes_when_internal_corner_has_clear_space(tmp_path: Path):
    path = tmp_path / "safe.dxf"
    _write(path, [_part(1, "L", _l_shape())])

    report = audit_nested_saw_clearance(
        path,
        diameter_mm=350,
        thickness_mm=20,
    )

    assert report["safe"] is True
    assert report["points_insufficient"] == 0
    assert report["min_clearance_mm"] == 84.947
