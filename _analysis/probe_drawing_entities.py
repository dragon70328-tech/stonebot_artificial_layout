# -*- coding: utf-8 -*-
"""图纸实体普查探针：实体类型/图层分布 + 文字样本（用于判断编号来源图层）。

用法：
  python -X utf8 _analysis/probe_drawing_entities.py <dxf路径> [文字样本数]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ezdxf  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    sample_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    by_type: Counter = Counter()
    by_layer: Counter = Counter()
    text_layers: Counter = Counter()
    samples: list[str] = []

    for entity in msp:
        entity_type = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        by_type[entity_type] += 1
        by_layer[layer] += 1
        if entity_type in {"TEXT", "MTEXT"}:
            text_layers[layer] += 1
            if len(samples) < sample_limit:
                content = (
                    entity.dxf.text
                    if entity_type == "TEXT"
                    else entity.text
                )
                content = str(content).replace("\n", " / ").strip()
                height = entity.dxf.get("height", None)
                samples.append(
                    f"[{entity_type}] layer={layer!r} "
                    f"h={height} {content[:60]!r}"
                )

    print(f"文件: {path}")
    print(f"modelspace 实体总数: {sum(by_type.values())}")
    print(f"按类型: {dict(by_type.most_common())}")
    print()
    print(f"按图层（前 20）: {dict(by_layer.most_common(20))}")
    print()
    print(f"TEXT/MTEXT 总数: {sum(text_layers.values())}")
    print(f"文字所在图层: {dict(text_layers.most_common())}")
    print()
    print(f"文字样本（最多 {sample_limit} 条）:")
    for line in samples:
        print("  " + line)

    layers = sorted(layer.dxf.name for layer in doc.layers)
    print()
    print(f"图层表（{len(layers)} 个）: {layers[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
