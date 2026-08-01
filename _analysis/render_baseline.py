"""渲染当前排板结果：每张大板一张 PNG，用于诊断浪费模式"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import box

from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts
from _analysis.viz import Canvas, part_color


def main():
    parts_data, doc = read_dxf(r'sample/countertop1_clean.dxf')
    parts = assign_numbers(parts_data)
    result = nest_parts(parts, 3200, 1800, 18, unit='metric')

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'new_sheets')
    os.makedirs(outdir, exist_ok=True)

    for sheet in result.sheets:
        c = Canvas(3200, 1800, scale=0.3)
        c.fill_polygon(box(0, 0, 3200, 1800), (245, 245, 245), edge=(0, 0, 0))
        for part in sheet.parts:
            c.fill_polygon(part.outer_polygon, part_color(part.number))
        c.save(os.path.join(outdir, f'sheet_{sheet.index:02d}.png'))

    # 总览图：所有大板拼成网格
    import math
    cols = 5
    rows = math.ceil(len(result.sheets) / cols)
    gap = 200
    cell_w, cell_h = 3200 + gap, 1800 + gap
    c = Canvas(cell_w * cols, cell_h * rows, scale=0.12)
    for i, sheet in enumerate(result.sheets):
        col, row = i % cols, i // cols
        ox = col * cell_w
        oy = (rows - 1 - row) * cell_h  # Sheet 1 在左上
        cc = Canvas(3200, 1800, scale=0.12)
        sheet_bg = box(ox, oy, ox + 3200, oy + 1800)
        c.fill_polygon(sheet_bg, (240, 240, 240), edge=(0, 0, 0))
        for part in sheet.parts:
            from shapely import affinity
            p = affinity.translate(part.outer_polygon, xoff=ox, yoff=oy)
            c.fill_polygon(p, part_color(part.number))
    c.save(os.path.join(outdir, 'overview.png'))

    print(f'rendered {result.total_sheets} sheets to {outdir}')


if __name__ == '__main__':
    main()
