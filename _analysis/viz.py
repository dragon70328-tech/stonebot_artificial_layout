"""极简 PNG 渲染器（纯标准库）：将 shapely 多边形渲染为 PNG，用于排板结果诊断"""

import struct
import zlib


def write_png(path, width, height, pixels):
    """pixels: bytearray, RGB 格式，按行存储"""
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        c += struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        return c

    raw = b''.join(b'\x00' + bytes(pixels[y * width * 3:(y + 1) * width * 3])
                   for y in range(height))
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 6))
    png += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)


class Canvas:
    """世界坐标 -> 像素的扫描线填充画布"""

    def __init__(self, world_w, world_h, scale=0.25, bg=(255, 255, 255),
                 origin=(0.0, 0.0)):
        self.width = int(world_w * scale) + 2
        self.height = int(world_h * scale) + 2
        self.scale = scale
        self.ox, self.oy = origin
        self.pixels = bytearray(self.width * self.height * 3)
        for i in range(0, len(self.pixels), 3):
            self.pixels[i:i + 3] = bytes(bg)

    def _to_px(self, x, y):
        return ((x - self.ox) * self.scale + 1,
                self.height - 1 - ((y - self.oy) * self.scale + 1))

    def fill_rings(self, rings, color):
        """even-odd 填充多个环（外环 + 孔）"""
        all_pts = [[self._to_px(x, y) for x, y in ring] for ring in rings]
        flat = [p for ring in all_pts for p in ring]
        if not flat:
            return
        ys = [p[1] for p in flat]
        y0 = max(0, int(min(ys)))
        y1 = min(self.height - 1, int(max(ys)) + 1)
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            xs = []
            for pts in all_pts:
                n = len(pts)
                for i in range(n):
                    xa, ya = pts[i]
                    xb, yb = pts[(i + 1) % n]
                    if (ya <= yc < yb) or (yb <= yc < ya):
                        t = (yc - ya) / (yb - ya)
                        xs.append(xa + t * (xb - xa))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                xa = max(0, int(xs[i]))
                xb = min(self.width - 1, int(xs[i + 1]) + 1)
                for x in range(xa, xb):
                    idx = (y * self.width + x) * 3
                    self.pixels[idx:idx + 3] = bytes(color)

    def draw_ring(self, ring, color, thick=1):
        pts = [self._to_px(x, y) for x, y in ring]
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
            for s in range(steps + 1):
                t = s / steps
                x = int(x0 + t * (x1 - x0))
                y = int(y0 + t * (y1 - y0))
                for dy in range(-(thick // 2), thick // 2 + 1):
                    for dx in range(-(thick // 2), thick // 2 + 1):
                        xx, yy = x + dx, y + dy
                        if 0 <= xx < self.width and 0 <= yy < self.height:
                            idx = (yy * self.width + xx) * 3
                            self.pixels[idx:idx + 3] = bytes(color)

    def fill_polygon(self, poly, color, edge=(0, 0, 0)):
        rings = [list(poly.exterior.coords)]
        rings += [list(r.coords) for r in poly.interiors]
        self.fill_rings(rings, color)
        if edge is not None:
            self.draw_ring(rings[0], edge)
            for r in rings[1:]:
                self.draw_ring(r, edge)

    def save(self, path):
        write_png(path, self.width, self.height, self.pixels)


def part_color(number):
    """由编号生成稳定颜色"""
    h = 0
    for ch in number:
        h = (h * 31 + ord(ch)) & 0xffffffff
    r = 80 + (h & 0x7f)
    g = 80 + ((h >> 8) & 0x7f)
    b = 80 + ((h >> 16) & 0x7f)
    return (r, g, b)
