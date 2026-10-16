"""在指定代码树里复算若干图纸的读图件数（历史二分定位用）。

用法：

    python count_drawing.py <代码树根> mercury="<源图.dxf>" fuzhou="<源图.dxf>"

代码树用 ``git archive <sha> src drawing_profiles | tar -x`` 导出，因此可以
在不动仓库 .git 的前提下复现任意历史提交的读图行为。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))

    from src.drawing_profile import (  # noqa: PLC0415
        load_profile,
        read_dxf_with_profile,
    )

    for spec in sys.argv[2:]:
        name, _, source = spec.partition("=")
        profile = load_profile(root / "drawing_profiles" / f"{name}.json")
        try:
            parts, _doc = read_dxf_with_profile(source, profile)
        except Exception as exc:  # 老提交的接口/依赖可能不同
            print(f"{name}: ERROR {type(exc).__name__}: {exc}")
            continue
        holes = sum(len(part.get("holes") or []) for part in parts)
        numbers = sum(1 for part in parts if part.get("original_number"))
        print(f"{name}: panels={len(parts)} holes={holes} numbers={numbers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
