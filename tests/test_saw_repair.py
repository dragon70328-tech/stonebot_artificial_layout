from pathlib import Path

from shapely.geometry import Polygon, box

from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.saw_audit import audit_nested_saw_clearance
from src.saw_repair import (
    repair_nested_saw_clearance,
    repair_nesting_result_saw_clearance,
)


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


def _write_risky(path: Path) -> None:
    parts = [
        _part(1, "L", _l_shape()),
        _part(2, "BLOCK", box(120, 120, 150, 150)),
    ]
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
        total_parts=2,
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=600000,
    )
    write_nested_dxf(result, str(path))


def test_repair_moves_part_without_changing_sheet_count(tmp_path: Path):
    source = tmp_path / "risk.dxf"
    output = tmp_path / "repaired.dxf"
    _write_risky(source)

    before = audit_nested_saw_clearance(
        source,
        diameter_mm=350,
        thickness_mm=20,
    )
    result = repair_nested_saw_clearance(
        source,
        output,
        diameter_mm=350,
        thickness_mm=20,
        max_moves_per_sheet=6,
    )
    after = audit_nested_saw_clearance(
        output,
        diameter_mm=350,
        thickness_mm=20,
    )

    assert before["safe"] is False
    assert after["safe"] is True
    assert before["sheet_count"] == after["sheet_count"] == 1
    assert before["part_count"] == after["part_count"] == 2
    assert result.updated_sheets[0]["sheet"] == 1
    assert result.diagnosis["safe"] is True


def test_repair_leaves_sheet_unresolved_when_move_budget_is_zero(tmp_path: Path):
    source = tmp_path / "risk.dxf"
    output = tmp_path / "unchanged.dxf"
    _write_risky(source)

    result = repair_nested_saw_clearance(
        source,
        output,
        diameter_mm=350,
        thickness_mm=20,
        max_moves_per_sheet=0,
    )

    assert result.improved is False
    assert result.unresolved_sheets
    assert result.diagnosis["safe"] is False
    assert output.exists()


def test_in_memory_repair_updates_part_geometry_without_changing_inventory():
    parts = [
        _part(1, "L", _l_shape()),
        _part(2, "BLOCK", box(120, 120, 150, 150)),
    ]
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
        total_parts=2,
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=600000,
    )
    before_bounds = [part.outer_polygon.bounds for part in parts]

    summary = repair_nesting_result_saw_clearance(
        result,
        clearance_mm=84.947,
        max_moves_per_sheet=6,
    )

    assert summary.improved is True
    assert summary.after_insufficient == 0
    assert result.total_sheets == 1
    assert result.total_parts == 2
    assert [part.number for part in parts] == ["L", "BLOCK"]
    assert [part.outer_polygon.bounds for part in parts] != before_bounds
