from __future__ import annotations

from pathlib import Path
import sys

import ezdxf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.layout_adjustment import _read_nested_layout
from src.saw_process import calculate_saw_clearance, diagnose_saw_access


def main() -> None:
    path = (
        ROOT
        / "_analysis"
        / "fremont_current_3400x1400"
        / "户型20261102_ST1-ST2_排板_3400x1400_192张_圆盘锯350.dxf"
    )
    clearance = calculate_saw_clearance(350, 20)
    sheets, _groups = _read_nested_layout(ezdxf.readfile(str(path)))
    rows = []
    for sheet in sheets:
        summary = diagnose_saw_access(
            [sheet],
            clearance.clearance_mm,
        )
        if summary.points_checked:
            rows.append(
                (
                    summary.min_clearance_mm or 0.0,
                    sheet.index,
                    summary.points_checked,
                    summary.points_insufficient,
                )
            )
    for minimum, index, checked, insufficient in sorted(rows)[:20]:
        print(
            f"Sheet_{index}: min={minimum:.3f}mm "
            f"checked={checked} insufficient={insufficient}"
        )

    sheet_149 = next(sheet for sheet in sheets if sheet.index == 149)
    summary_149 = diagnose_saw_access(
        [sheet_149],
        clearance.clearance_mm,
    )
    print(
        "SHEET_149 "
        f"checked={summary_149.points_checked} "
        f"insufficient={summary_149.points_insufficient} "
        f"min={summary_149.min_clearance_mm}"
    )
    print(
        "SHEET_149_PARTS "
        + ", ".join(part.number for part in sheet_149.parts)
    )


if __name__ == "__main__":
    main()
