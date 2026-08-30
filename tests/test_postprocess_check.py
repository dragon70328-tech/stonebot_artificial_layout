from shapely.geometry import box

from src.models import Part, Sheet
from src.postprocess import diagnose_postprocess, diagnose_waterjet


def _part(number, x0, y0, x1, y1):
    poly = box(x0, y0, x1, y1)
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=((x0 + x1) / 2.0, (y0 + y1) / 2.0),
    )


def test_diagnose_reports_part_not_on_edge():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 100.0, 100.0),
            _part("B", 200.0, 50.0, 300.0, 150.0),
        ],
    )
    warnings = diagnose_postprocess(
        [sheet],
        slide_expected=True,
        align_expected=False,
    )

    edge_warnings = [w for w in warnings if w["type"] == "part_not_on_edge"]
    assert len(edge_warnings) == 1
    assert edge_warnings[0]["sheet"] == 1
    assert edge_warnings[0]["parts"] == ["B"]


def test_diagnose_does_not_report_blocked_interior_part_as_slide_failure():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 100.0, 1000.0),
            _part("C", 900.0, 0.0, 1000.0, 1000.0),
            _part("D", 0.0, 0.0, 1000.0, 100.0),
            _part("E", 0.0, 900.0, 1000.0, 1000.0),
            _part("B", 200.0, 200.0, 300.0, 300.0),
        ],
    )
    warnings = diagnose_postprocess(
        [sheet],
        slide_expected=True,
        align_expected=False,
    )

    edge_warnings = [w for w in warnings if w["type"] == "part_not_on_edge"]
    assert edge_warnings == []


def test_diagnose_reports_unaligned_through_cut_edges():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 100.0, 300.0),
            _part("B", 100.5, 0.0, 200.5, 300.0),
        ],
    )
    warnings = diagnose_postprocess(
        [sheet],
        slide_expected=False,
        align_expected=True,
    )

    cut_warnings = [
        w for w in warnings if w["type"] == "through_cut_not_aligned"
    ]
    assert len(cut_warnings) == 1
    assert cut_warnings[0]["sheet"] == 1
    assert cut_warnings[0]["edges"][0]["parts"] == ["A", "B"]


def test_diagnose_reports_min_gap_violation():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 100.0, 100.0),
            _part("B", 105.0, 0.0, 205.0, 100.0),
        ],
    )
    warnings = diagnose_postprocess(
        [sheet],
        slide_expected=False,
        align_expected=False,
        gap_mm=10.0,
    )

    gap_warnings = [w for w in warnings if w["type"] == "min_gap_violation"]
    assert len(gap_warnings) == 1
    assert gap_warnings[0]["sheet"] == 1
    assert gap_warnings[0]["pairs"][0]["parts"] == ["A", "B"]


def test_diagnose_passes_when_goals_are_met():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 100.0, 300.0),
            _part("B", 100.0, 0.0, 200.0, 300.0),
        ],
    )
    warnings = diagnose_postprocess(
        [sheet],
        slide_expected=True,
        align_expected=True,
        gap_mm=0.0,
    )

    assert warnings == []


def test_diagnose_waterjet_reports_first_part_not_on_left_edge():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 50.0, 0.0, 70.0, 100.0),
            _part("B", 200.0, 0.0, 300.0, 20.0),
        ],
    )
    warnings, metrics = diagnose_waterjet(
        [sheet],
        first_part_left_edge=True,
        arbitrary_rotation=False,
        rotations=(0,),
    )

    edge_warnings = [
        w for w in warnings if w["type"] == "first_part_not_on_left_edge"
    ]
    assert len(edge_warnings) == 1
    assert edge_warnings[0]["part"] == "A"
    assert metrics["first_part_left_edge_failed"] == 1


def test_diagnose_waterjet_accepts_first_part_on_left_edge():
    sheet = Sheet(
        index=1,
        width=1000.0,
        height=1000.0,
        thickness=20.0,
        parts=[
            _part("A", 0.0, 0.0, 20.0, 100.0),
            _part("B", 200.0, 0.0, 300.0, 20.0),
        ],
    )
    warnings, metrics = diagnose_waterjet(
        [sheet],
        first_part_left_edge=True,
        arbitrary_rotation=False,
        rotations=(0,),
    )

    assert warnings == []
    assert metrics["first_part_left_edge_checked"] == 1
    assert metrics["first_part_left_edge_failed"] == 0
