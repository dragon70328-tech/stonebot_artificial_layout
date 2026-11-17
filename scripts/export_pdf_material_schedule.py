"""Export material-based specification sizes and quantities from an in-place DXF."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.pdf_intake.material_schedule import (  # noqa: E402
    build_material_schedule,
    write_material_schedule_xlsx,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("output_xlsx", type=Path)
    args = parser.parse_args()
    schedule = build_material_schedule(args.input_dxf)
    destination = write_material_schedule_xlsx(schedule, args.output_xlsx)
    print(destination)
    print(f"parts={len(schedule.parts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
