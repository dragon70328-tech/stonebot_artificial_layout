import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import box
from shapely import affinity

from src.models import Part, Sheet, NestingResult
from src.nesting import validate_nesting
from src.postprocess import PostProcessor
import main as app


def make_part(number: str, width: float, height: float,
              x: float = 0.0, y: float = 0.0) -> Part:
    poly = box(x, y, x + width, y + height)
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=((x + width) / 2.0, (y + height) / 2.0),
    )


def test_two_special_rectangles_share_one_sheet():
    parts = [make_part("S1", 400.0, 600.0),
             make_part("S2", 400.0, 600.0)]
    result = app.nest_special_parts(parts, 1000.0, 1000.0, 20.0,
                                    "metric", [0, 90, 180, 270])

    assert result.total_parts == 2
    assert result.total_sheets == 1
    assert validate_nesting(result, 1000.0, 1000.0) == []


def test_nine_special_rectangles_produce_five_sheets():
    parts = [make_part(f"S{i}", 400.0, 600.0) for i in range(1, 10)]
    result = app.nest_special_parts(parts, 1000.0, 1000.0, 20.0,
                                    "metric", [0, 90, 180, 270])

    assert result.total_parts == 9
    assert result.total_sheets == 5
    assert [len(sheet.parts) for sheet in result.sheets] == [2, 2, 2, 2, 1]
    assert validate_nesting(result, 1000.0, 1000.0) == []


def test_single_special_part_gets_own_sheet():
    parts = [make_part("S1", 700.0, 700.0)]
    result = app.nest_special_parts(parts, 1000.0, 1000.0, 20.0,
                                    "metric", [0, 90, 180, 270])

    assert result.total_sheets == 1
    assert len(result.sheets[0].parts) == 1
    assert validate_nesting(result, 1000.0, 1000.0) == []


def test_postprocessor_enforces_gap_when_room_exists():
    p1 = make_part("A", 400.0, 400.0, 100.0, 100.0)
    p2 = make_part("B", 400.0, 400.0, 550.0, 100.0)
    sheet = Sheet(index=1, width=1000.0, height=1000.0,
                  thickness=20.0, parts=[p1, p2])

    PostProcessor(1000.0, 1000.0).enforce_min_gap([sheet], 80.0)

    assert p1.outer_polygon.distance(p2.outer_polygon) >= 80.0 - 1e-6
    assert validate_nesting(NestingResult(
        sheets=[sheet], unit="metric", total_parts=2, total_sheets=1,
        total_part_area=sum(p.area for p in sheet.parts),
        total_sheet_area=1000.0 * 1000.0,
    ), 1000.0, 1000.0) == []


def test_postprocessor_slide_keeps_parts_inside_sheet():
    p1 = make_part("A", 400.0, 600.0, 100.0, 100.0)
    p2 = make_part("B", 400.0, 600.0, 600.0, 100.0)
    sheet = Sheet(index=1, width=1000.0, height=1000.0,
                  thickness=20.0, parts=[p1, p2])

    PostProcessor(1000.0, 1000.0).run([sheet], slide=True, align=True, gap_mm=0.0)
    errors = validate_nesting(NestingResult(
        sheets=[sheet], unit="metric", total_parts=2, total_sheets=1,
        total_part_area=sum(p.area for p in sheet.parts),
        total_sheet_area=1000.0 * 1000.0,
    ), 1000.0, 1000.0)

    assert errors == []
    for part in sheet.parts:
        minx, miny, maxx, maxy = part.outer_polygon.bounds
        assert minx >= -1e-6
        assert miny >= -1e-6
        assert maxx <= 1000.0 + 1e-6
        assert maxy <= 1000.0 + 1e-6


def test_postprocessor_through_cut_aligns_nearly_collinear_edges():
    p1 = make_part("A", 100.0, 100.0, 100.0, 100.0)
    p2 = make_part("B", 100.0, 100.0, 101.0, 300.0)
    sheet = Sheet(index=1, width=1000.0, height=1000.0,
                  thickness=20.0, parts=[p1, p2])

    PostProcessor(1000.0, 1000.0).through_cut([sheet])

    assert abs(p1.outer_polygon.bounds[2] - p2.outer_polygon.bounds[2]) < 1e-6
    assert validate_nesting(NestingResult(
        sheets=[sheet], unit="metric", total_parts=2, total_sheets=1,
        total_part_area=sum(p.area for p in sheet.parts),
        total_sheet_area=1000.0 * 1000.0,
    ), 1000.0, 1000.0) == []


def test_parse_special_size_accepts_x_separator():
    assert app.parse_special_size("3225x1625") == (3225.0, 1625.0)


def test_lwpolyline_bulge_is_sampled_not_chorded():
    import ezdxf
    from src.dxf_reader import _lwpolyline_points

    doc = ezdxf.new()
    lw = doc.modelspace().add_lwpolyline(
        [(0.0, 0.0, 0.0, 0.0, 0.4142135623730951), (100.0, 0.0)],
        close=False,
    )

    points = _lwpolyline_points(lw)

    assert len(points) > 4
    assert abs(points[0][0]) < 1e-6
    assert abs(points[0][1]) < 1e-6
    assert abs(points[-1][0] - 100.0) < 1e-6
    assert abs(points[-1][1]) < 1e-6
    mid = points[len(points) // 2]
    assert 0.0 < mid[0] < 100.0
    assert abs(mid[1]) > 1.0
