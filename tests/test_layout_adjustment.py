from pathlib import Path

import ezdxf
from shapely.geometry import box

from src.dxf_writer import write_nested_dxf
from src.layout_adjustment import (
    _read_nested_layout,
    propagate_adjusted_layout,
)
from src.models import NestingResult, Part, Sheet


def _part(part_id: int, number: str, x: float, y: float) -> Part:
    polygon = box(x, y, x + 200, y + 100)
    return Part(
        id=part_id,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        area=polygon.area,
        label_position=(x + 100, y + 50),
    )


def _result() -> NestingResult:
    sheets = []
    for index in range(1, 4):
        parts = [
            _part(index * 10 + 1, "A", 0, 0),
            _part(index * 10 + 2, "B", 200, 0),
        ]
        sheets.append(
            Sheet(
                index=index,
                width=1000,
                height=600,
                thickness=20,
                parts=parts,
            )
        )
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=6,
        total_sheets=3,
        total_part_area=sum(part.area for sheet in sheets for part in sheet.parts),
        total_sheet_area=3 * 1000 * 600,
    )


def test_propagates_translated_reference_sheet_to_identical_inventory(tmp_path: Path):
    initial = tmp_path / "initial.dxf"
    adjusted = tmp_path / "adjusted.dxf"
    output = tmp_path / "propagated.dxf"
    write_nested_dxf(_result(), str(initial))

    doc = ezdxf.readfile(str(initial))
    sheets, _groups = _read_nested_layout(doc)
    reference = next(sheet for sheet in sheets if sheet.index == 2)
    by_number = {part.number: part for part in reference.parts}
    for entity in by_number["A"].entities:
        entity.translate(0, 200, 0)
    for entity in by_number["B"].entities:
        entity.translate(100, 200, 0)
    doc.saveas(adjusted)

    result = propagate_adjusted_layout(
        adjusted,
        reference_sheet_index=2,
        output_dxf=output,
    )

    assert result.reference_sheet_index == 2
    assert result.updated_sheet_indices == [1, 3]
    assert result.moved_group_count == 4
    assert result.skipped_sheets == []

    propagated, _groups = _read_nested_layout(ezdxf.readfile(str(output)))
    for index in (1, 3):
        sheet = next(item for item in propagated if item.index == index)
        parts = {part.number: part for part in sheet.parts}
        assert parts["A"].bounds[:2] == (0.0, 200.0)
        assert parts["B"].bounds[:2] == (300.0, 200.0)

    reference_after, _groups = _read_nested_layout(
        ezdxf.readfile(str(output))
    )
    reference_sheet = next(
        item for item in reference_after if item.index == 2
    )
    reference_parts = {
        part.number: part for part in reference_sheet.parts
    }
    assert reference_parts["A"].bounds[:2] == (0.0, 200.0)
    assert reference_parts["B"].bounds[:2] == (300.0, 200.0)


def test_invalid_reference_layout_is_rejected_before_output(tmp_path: Path):
    initial = tmp_path / "initial.dxf"
    adjusted = tmp_path / "adjusted.dxf"
    output = tmp_path / "propagated.dxf"
    write_nested_dxf(_result(), str(initial))

    doc = ezdxf.readfile(str(initial))
    sheets, _groups = _read_nested_layout(doc)
    reference = next(sheet for sheet in sheets if sheet.index == 1)
    for entity in reference.parts[1].entities:
        entity.translate(-500, 0, 0)
    doc.saveas(adjusted)

    try:
        propagate_adjusted_layout(
            adjusted,
            reference_sheet_index=1,
            output_dxf=output,
        )
    except ValueError as exc:
        assert "reference sheet 1 is invalid" in str(exc)
    else:
        raise AssertionError("invalid reference sheet should be rejected")
    assert not output.exists()
