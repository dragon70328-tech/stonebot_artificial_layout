"""最小复现：末点落在已有边上 0.031mm 的 8 顶点多段线（水星零件的结构）。

用法：python repro_closure.py <代码树根> [...]
验证 closed_tolerance=0.1 的画像能不能把它读成 1 件。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import ezdxf

# 取自水星 11DA7（F-3厨房-01）：v7 距 v1 仅 0.0309mm
POINTS = [
    (44456.5980, -92078.4183),
    (44456.5980, -92722.9433),
    (46077.4355, -92722.9433),
    (46077.4355, -92078.4183),
    (45101.1230, -92078.4183),
    (45101.1230, -90182.9742),
    (44456.5980, -90182.9742),
    (44456.5980, -92722.9742),
]


def main() -> int:
    for root_arg in sys.argv[1:]:
        root = Path(root_arg).resolve()
        sys.path.insert(0, str(root))
        for module in [key for key in list(sys.modules) if key.startswith("src")]:
            del sys.modules[module]

        from src.drawing_profile import (  # noqa: PLC0415
            DrawingProfile,
            read_dxf_with_profile,
        )

        path = Path(tempfile.mkdtemp()) / "repro.dxf"
        doc = ezdxf.new("R2010")
        doc.layers.add("PANEL")
        doc.modelspace().add_lwpolyline(
            POINTS, dxfattribs={"layer": "PANEL"}
        )
        doc.saveas(path)

        for tolerance in (0.1, 0.01):
            profile = DrawingProfile.from_dict(
                {
                    "name": "repro",
                    "version": "1.0.0",
                    "panel_layer": "PANEL",
                    "panel_layers": ["PANEL"],
                    "closed_tolerance": tolerance,
                    "build_hierarchy": False,
                    "number_layers": [],
                }
            )
            parts, _doc = read_dxf_with_profile(path, profile)
            print(
                f"{root.name:12s} closed_tolerance={tolerance:<5} "
                f"-> panels={len(parts)}"
            )
        sys.path.remove(str(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
