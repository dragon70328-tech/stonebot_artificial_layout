from pathlib import Path

import ezdxf
from shapely.geometry import Polygon

from src.bridge_template_learning import (
    learn_template_boards,
    reuse_template_boards,
)
from src.deepnest_engine import _rotation_forms as deepnest_rotation_forms
from src.models import Part
from src.nesting import _rotation_forms as bridge_rotation_forms


def _part(
    part_id: int,
    number: str,
    polygon: Polygon,
    allowed: tuple[float, ...] | None,
) -> Part:
    return Part(
        id=part_id,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        area=polygon.area,
        allowed_rotations=allowed,
    )


def _write_template(path: Path) -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 600), (0, 600)],
        close=True,
    )
    msp.add_lwpolyline(
        [(0, 0), (600, 0), (600, 200), (0, 200)],
        close=True,
    )
    msp.add_lwpolyline(
        [(620, 0), (820, 0), (820, 400), (620, 400)],
        close=True,
    )
    doc.saveas(path)


def test_part_level_rotation_override_is_honored() -> None:
    polygon = Polygon([(0, 0), (80, 0), (80, 20), (0, 20)])
    part = _part(1, "P-1", polygon, (90.0,))

    bridge_forms = bridge_rotation_forms(part, {}, (0, 90, 180, 270))
    deepnest_forms = deepnest_rotation_forms(part, [0, 90, 180, 270], {})

    assert [item[0] for item in bridge_forms] == [90.0]
    assert [item[0] for item in deepnest_forms] == [90.0]
    assert abs(
        bridge_forms[0][2][2] - bridge_forms[0][2][0] - 20.0
    ) < 1e-6


def test_learn_and_reuse_compatible_template_board(tmp_path: Path) -> None:
    template_path = tmp_path / "template.dxf"
    _write_template(template_path)

    first = Polygon([(0, 0), (600, 0), (600, 200), (0, 200)])
    second_source = Polygon([(0, 0), (400, 0), (400, 200), (0, 200)])
    extra = Polygon([(0, 0), (50, 0), (50, 40), (0, 40)])
    parts = [
        _part(1, "A-1", first, (0.0,)),
        _part(2, "A-2", second_source, (90.0,)),
        _part(3, "A-3", extra, (0.0,)),
    ]

    templates, summary = learn_template_boards(
        template_path,
        1000.0,
        600.0,
    )
    reused = reuse_template_boards(
        parts,
        templates,
        fallback_rotations=(0.0, 90.0, 180.0, 270.0),
        shape_tolerance=0.2,
    )

    assert summary["nonempty_recipe_count"] == 1
    assert len(reused.sheets) == 1
    assert {item.part.number for item in reused.sheets[0]} == {"A-1", "A-2"}
    assert [item.number for item in reused.remaining_parts] == ["A-3"]
    assert reused.stats["template_sheets_rejected"] == 0


def test_template_reuse_rejects_disallowed_orientation(tmp_path: Path) -> None:
    template_path = tmp_path / "template.dxf"
    _write_template(template_path)

    first = Polygon([(0, 0), (600, 0), (600, 200), (0, 200)])
    second_source = Polygon([(0, 0), (400, 0), (400, 200), (0, 200)])
    parts = [
        _part(1, "A-1", first, (0.0,)),
        _part(2, "A-2", second_source, (0.0,)),
    ]

    templates, _summary = learn_template_boards(
        template_path,
        1000.0,
        600.0,
    )
    reused = reuse_template_boards(
        parts,
        templates,
        fallback_rotations=(0.0, 90.0, 180.0, 270.0),
        shape_tolerance=0.2,
    )

    assert reused.sheets == []
    assert reused.stats["template_sheets_rejected"] == 1
    assert len(reused.remaining_parts) == 2
