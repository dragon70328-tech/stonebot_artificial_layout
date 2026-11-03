from pathlib import Path

from shapely.geometry import Polygon

from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf


def test_readback_locates_part_using_physical_oversize_margin(tmp_path: Path) -> None:
    polygon = Polygon(
        [(0.0, 1999.0), (900.0, 1999.0), (900.0, 2003.0), (0.0, 2003.0)]
    )
    part = Part(
        id=1,
        number="C1-ST/1-001",
        polygon=polygon,
        outer_polygon=polygon,
        material_group="ST-1+ST-2",
        material_column="ST-1+ST-2",
        area=polygon.area,
        label_position=(450.0, 2001.0),
        source_outer_polygon=polygon,
        placement_rot=0.0,
    )
    sheet = Sheet(
        index=1,
        width=3000.0,
        height=2000.0,
        thickness=20.0,
        parts=[part],
    )
    result = NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=1,
        total_sheets=1,
        total_part_area=polygon.area,
        total_sheet_area=6_000_000.0,
    )
    output = tmp_path / "oversize.dxf"
    write_nested_dxf(
        result,
        output,
        unit_system="metric",
        board_size=(3000.0, 2000.0),
    )

    assert verify_nested_dxf(
        output,
        sheets_payload_from_result(result),
    ) == []
