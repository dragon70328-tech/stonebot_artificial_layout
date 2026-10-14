"""Inventory decompiled type names per GstoneCAD module and flag likely
nesting/layout related code.

Usage:
    python -X utf8 _analysis/rea_youshi/inventory_types.py [keyword ...]
"""

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_KEYWORDS = [
    "nest", "layout", "pack", "sort", "optim", "sheet", "board", "slab",
    "stone", "cut", "place", "arrange", "area", "strip", "rect", "poly",
    "dcl", "bh", "pol", "row", "util", "plan", "auto", "calc", "algorithm",
]


def project_dirs():
    return sorted(p for p in glob.glob(os.path.join(ROOT, "proj", "*")) if os.path.isdir(p))


def main():
    keywords = [k.lower() for k in (sys.argv[1:] or DEFAULT_KEYWORDS)]
    for proj in project_dirs():
        cs = [p for p in glob.glob(os.path.join(proj, "**", "*.cs"), recursive=True)]
        names = [os.path.basename(p)[:-3] for p in cs]
        print(f"\n===== {os.path.basename(proj)}: {len(cs)} types/files =====")
        obf = [n for n in names if re.fullmatch(r"[A-Za-z0-9_`<>{}]{1,4}(\d*)?", n) and not n[0].isupper()]
        cjk = [n for n in names if re.search("[\u4e00-\u9fff]", n)]
        print(f"obfuscated-looking: {len(obf)}   CJK-named: {len(cjk)}")
        for kw in keywords:
            hit = sorted(n for n in names if kw in n.lower())
            if hit:
                print(f"  [{kw}] {len(hit)}: {', '.join(hit[:24])}")


if __name__ == "__main__":
    main()
