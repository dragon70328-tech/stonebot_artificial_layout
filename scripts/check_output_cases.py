"""Run read + nest + postprocess checks across diverse output DXF cases.

This is intentionally not a pytest module: the source DXF files live in the
local ``output/`` directory and can be large.  It uses ``check_only=True`` so
the final nested DXF/report is not written; the per-case post-process check
JSON is still produced and the script exits non-zero on geometry failures.
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import main as app
from src.constraints import PROFILE_MIN_SHEETS, PROFILE_WATERJET
from src.units import UnitSystem


CASES = [
    {
        "name": "canada3l_bridge",
        "dxf": "output/20260829_164306_加拿大3L/加拿大3L_numbered_原位.dxf",
        "width": 3230.0,
        "height": 1830.0,
        "profile": PROFILE_MIN_SHEETS.with_overrides(min_gap=5.0),
        "special": None,
    },
    {
        "name": "cgr45_bridge",
        "dxf": "output/20260815_144424_CGR45final/CGR45final_numbered_原位.dxf",
        "width": 3200.0,
        "height": 1800.0,
        "profile": PROFILE_MIN_SHEETS,
        "special": None,
    },
    {
        "name": "blockd_bridge",
        "dxf": "output/20260828_145844_Block D - 6F大堂地台石 廠方切石圖/Block D - 6F大堂地台石 廠方切石圖_numbered_原位.dxf",
        "width": 3200.0,
        "height": 1800.0,
        "profile": PROFILE_MIN_SHEETS,
        "special": None,
    },
    {
        "name": "mercury_bridge",
        "dxf": "output/20260830_185527_水星项目户型及排板/水星项目户型及排板_numbered_原位.dxf",
        "width": 3200.0,
        "height": 1600.0,
        "profile": PROFILE_MIN_SHEETS,
        "special": (3225.0, 1625.0),
    },
    {
        "name": "outlets1_waterjet",
        "dxf": "output/20260820_181251_东莞奥特莱斯1/东莞奥特莱斯1_numbered_原位.dxf",
        "width": 3200.0,
        "height": 1600.0,
        "profile": PROFILE_WATERJET.with_overrides(
            rotation=[0],
            arbitrary_rotation=False,
            first_part_left_edge=True,
        ),
        "special": None,
    },
    {
        "name": "outlets2_waterjet",
        "dxf": "output/20260820_180030_东莞奥特莱斯2/东莞奥特莱斯2_numbered_原位.dxf",
        "width": 3200.0,
        "height": 1600.0,
        "profile": PROFILE_WATERJET.with_overrides(
            rotation=[0],
            arbitrary_rotation=False,
            first_part_left_edge=True,
        ),
        "special": None,
    },
]


def main() -> int:
    failed = []
    args = [arg for arg in sys.argv[1:]]
    require_all = "--require-all" in args
    only = {arg for arg in args if not arg.startswith("--")}
    selected = [case for case in CASES if not only or case["name"] in only]
    for case in selected:
        dxf_path = PROJECT_ROOT / case["dxf"]
        if not dxf_path.exists():
            if require_all:
                print(f"[FAIL] {case['name']}: missing {dxf_path}")
                failed.append(case["name"])
            else:
                print(f"[SKIP] {case['name']}: missing {dxf_path}")
            continue

        print(f"[RUN ] {case['name']}")
        try:
            outcome = app.run(
                str(dxf_path),
                case["width"],
                case["height"],
                20.0,
                unit=UnitSystem.METRIC,
                trials=1,
                seed=0,
                budget=0.0,
                skip_unnumbered=True,
                layers=None,
                exclude_layers=None,
                profile=case["profile"],
                confirm_sheet_count=False,
                report_only=False,
                special_size=case["special"],
                check_only=True,
                quick=True,
                pairing=False,
            )
            errors = (outcome or {}).get("geometry_errors", [])
            warnings = (outcome or {}).get("postprocess_warnings", [])
            if errors:
                print(f"[FAIL] {case['name']}: {len(errors)} geometry errors")
                failed.append(case["name"])
            else:
                print(
                    f"[PASS] {case['name']}: geometry ok, "
                    f"{len(warnings)} postprocess warnings"
                )
        except Exception as exc:  # noqa: BLE001 - operational report
            print(f"[FAIL] {case['name']}: {exc}")
            failed.append(case["name"])

    if failed:
        print(f"\n{len(failed)} failed: {', '.join(failed)}")
        return 1
    print("\nAll available cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
