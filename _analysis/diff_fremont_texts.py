from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path

import ezdxf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = collections.Counter(
        str(entity.dxf.text)
        for entity in ezdxf.readfile(args.source).modelspace()
        if entity.dxftype() == "TEXT"
    )
    output = collections.Counter(
        str(entity.dxf.text)
        for entity in ezdxf.readfile(args.output).modelspace()
        if entity.dxftype() == "TEXT"
        and re.match(r"^(UNIT TYPE|材料|L[2-5]$|[ABC]户型)", str(entity.dxf.text))
    )
    print("source", sum(source.values()), len(source))
    print("matched", sum(output.values()), len(output))
    print("missing", source - output)
    print("extra", output - source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
