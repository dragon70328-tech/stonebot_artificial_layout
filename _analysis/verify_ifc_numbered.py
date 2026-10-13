# -*- coding: utf-8 -*-
"""回读校验生成的加拿大IFC原位编号 DXF，并核对编号明细。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import ezdxf

numbered = Path(sys.argv[1])
recognized = Path(sys.argv[2])

print("=" * 62)
print("A) 回读原位编号 DXF")
print("=" * 62)
print(f"文件: {numbered}")
print(f"大小: {numbered.stat().st_size:,} bytes")
doc = ezdxf.readfile(str(numbered))
msp = doc.modelspace()
by_type = Counter(e.dxftype() for e in msp)
by_layer = Counter(e.dxf.layer for e in msp)
print(f"实体总数: {len(msp)}")
print(f"  按类型: {dict(by_type)}")
print(f"  按图层: {dict(by_layer)}")
print(f"DXF 版本: {doc.dxfversion}  单位(INSUNITS): {doc.header.get('$INSUNITS')}")
layers = sorted(l.dxf.name for l in doc.layers)
print(f"图层清单: {layers}")

# 与原图坐标范围对比，确认是原位输出（未平移旋转）
bb = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
print(f"图纸范围 EXTMIN/EXTMAX: {bb}")

print()
print("=" * 62)
print("B) 编号明细（来自 recognized.json）")
print("=" * 62)
data = json.loads(recognized.read_text(encoding="utf-8"))
panels = data.get("panels", [])
nums = [p.get("number") for p in panels]
has_num = [n for n in nums if n]
no_num = [p for p in panels if not p.get("number")]
suffixed = [n for n in has_num if n and n.rsplit("-", 1)[-1].isdigit()
            and len(n.rsplit("-", 1)[-1]) == 1 and n.rsplit("-", 1)[-1] != "0"]
dup = {n: c for n, c in Counter(has_num).items() if c > 1}
print(f"面板总数            : {len(panels)}")
print(f"有编号              : {len(has_num)}")
print(f"无编号              : {len(no_num)}")
print(f"重复编号(去重后)     : {dup}")
print(f"疑似追加后缀(-2等)   : {suffixed}")
print(f"孔洞字段合计        : {sum(len(p.get('holes') or []) for p in panels)}")

print()
print("=" * 62)
print("C) 交付位置")
print("=" * 62)
desktop = Path(r"C:\Users\drago\Desktop\临时文件")
print(f"源图纸目录: {desktop}")
print(f"  存在: {desktop.exists()}")
if desktop.exists():
    same = [p.name for p in desktop.glob('*IFC*')]
    print(f"  目录下 IFC 相关文件: {same}")
outdir = numbered.parent
for f in sorted(outdir.iterdir()):
    print(f"产物: {f.name}  ({f.stat().st_size:,} bytes)")
