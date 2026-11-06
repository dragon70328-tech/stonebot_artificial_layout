from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

import ezdxf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.layout_adjustment import _read_nested_layout
from src.saw_process import calculate_saw_clearance, diagnose_saw_access


FREMONT = ROOT / "_analysis" / "fremont_current_3400x1400"
ST8 = (
    ROOT
    / "_analysis"
    / "st8_opt_578_final"
    / "20261105_195508_citizen_ST-08_全部2018_numbered_原位"
)


def main() -> None:
    paths = [
        FREMONT / "户型20261102_ST1-ST2_排板_3400x1400.dxf",
        FREMONT / "户型20261102_ST1-ST2_排板_3400x1400_整数配比.dxf",
        FREMONT / "户型20261102_ST1-ST2_排板_3400x1400_整数配比_全候选.dxf",
        FREMONT / "户型20261102_ST1-ST2_排板_3400x1400_192张_回看.dxf",
        FREMONT / "user_edited_sheet149_recovered.dxf",
        FREMONT / "户型20261102_ST1-ST2_排板_3400x1400_192张_圆盘锯350.dxf",
        *sorted(ST8.glob("*_310共边.dxf")),
        *sorted(ST8.glob("*_成对共边.dxf")),
    ]
    clearance = calculate_saw_clearance(350, 20)
    print(f"CLEARANCE={clearance.clearance_mm:.3f}mm")
    for path in paths:
        print("FILE\t" + str(path))
        if not path.exists():
            print("  MISSING")
            continue
        try:
            sheets, _groups = _read_nested_layout(
                ezdxf.readfile(str(path))
            )
            summary = diagnose_saw_access(
                sheets,
                clearance.clearance_mm,
                max_warnings=10000,
            )
            pairs = Counter(
                (
                    warning["sheet"],
                    warning["part"],
                    warning["neighbor_part"],
                )
                for warning in summary.warnings
            )
            print(
                "  "
                f"sheets={len(sheets)} "
                f"parts={sum(len(sheet.parts) for sheet in sheets)} "
                f"checked={summary.points_checked} "
                f"insufficient={summary.points_insufficient} "
                f"min={summary.min_clearance_mm} "
                f"deficit={summary.total_deficit_mm:.3f}"
            )
            for (sheet, part, neighbor), count in sorted(pairs.items())[:30]:
                print(
                    f"  HIT Sheet_{sheet}: "
                    f"{part} -> {neighbor} x{count}"
                )
            if len(pairs) > 30:
                print(f"  ... {len(pairs) - 30} more part-pairs")
        except Exception as exc:  # noqa: BLE001 - diagnostic-only script
            print(f"  ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
