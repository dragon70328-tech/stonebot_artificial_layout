"""纯 Python 把排板布局栅格化成 PNG（无第三方绘图依赖）。

用法:
    from raster import dump_sheets_png
    dump_sheets_png(sheets_polys, "out.png", sheet_w=3200, sheet_h=1800)
sheets_polys: list[list[Polygon]]，每个 Polygon 为板局部坐标。
"""
import struct
import zlib

import numpy as np


def _write_png(path, img):
    """img: uint8 HxW 灰度数组。"""
    h, w = img.shape
    raw = b"".join(b"\x00" + img[i].tobytes() for i in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def _rasterize(poly, mask, scale):
    """把多边形填充(1)与边线(2)画进 mask。"""
    coords = np.asarray(poly.exterior.coords)
    px = coords[:, 0] * scale
    py = coords[:, 1] * scale
    minx = max(int(px.min()) - 1, 0)
    maxx = min(int(px.max()) + 2, mask.shape[1])
    miny = max(int(py.min()) - 1, 0)
    maxy = min(int(py.max()) + 2, mask.shape[0])
    if minx >= maxx or miny >= maxy:
        return
    pts = np.column_stack([px, py])
    for row in range(miny, maxy):
        y = row + 0.5
        xs = []
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if (y1 <= y < y2) or (y2 <= y < y1):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            a = max(int(xs[i]), minx)
            b = min(int(xs[i + 1]) + 1, maxx)
            if a < b:
                mask[row, a:b] = 1
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        n = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
        for t in range(n + 1):
            xi = int(x1 + (x2 - x1) * t / n)
            yi = int(y1 + (y2 - y1) * t / n)
            if 0 <= yi < mask.shape[0] and 0 <= xi < mask.shape[1]:
                mask[yi, xi] = 2


def dump_sheets_png(sheets_polys, path, sheet_w=3200.0, sheet_h=1800.0,
                    cols=None, scale=0.25, pad=8):
    """sheets_polys: list[list[Polygon]]，横向网格排列输出 PNG。"""
    n = len(sheets_polys)
    if cols is None:
        cols = min(n, 4)
    rows = (n + cols - 1) // cols
    cw, ch = int(sheet_w * scale), int(sheet_h * scale)
    gw, gh = cw + pad, ch + pad
    img = np.full((rows * gh + pad, cols * gw + pad), 255, dtype=np.uint8)
    for si, polys in enumerate(sheets_polys):
        r, c = divmod(si, cols)
        x0, y0 = pad + c * gw, pad + r * gh
        mask = np.zeros((ch, cw), dtype=np.uint8)
        for poly in polys:
            _rasterize(poly, mask, scale)
        block = img[y0:y0 + ch, x0:x0 + cw]
        block[mask == 1] = 200
        block[mask == 2] = 30
        block[0, :] = 0
        block[-1, :] = 0
        block[:, 0] = 0
        block[:, -1] = 0
    _write_png(path, img)
