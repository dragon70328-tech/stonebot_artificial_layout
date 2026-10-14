"""算术预判：把 k 张填充最低的板重排进 k-1 张，需要多高的填充率？

用于决定"多板联合重排（P1-1）"值不值得做：如果最小可行 k 对应的所需填充率
超过我们实测能达到的单板填充上限，那这条路就没有几何可行性。

用法：
    python -X utf8 _analysis/rea_youshi/probe_repack_headroom.py <report.json> [...]
"""

import glob
import json
import os
import sys

# 实测单板填充上限（CGR45 最高 96.8%，citizen 最高 96.8%；取 95% 作为乐观天花板）
ACHIEVABLE_FILL = 0.95


def analyse(report_path):
    report = json.load(open(report_path, encoding="utf-8"))
    dims = report["sheet_dimensions"]
    sheet_area = dims["usable_width"] * dims["usable_height"] / 1e6  # m²
    rec_path = glob.glob(os.path.join(os.path.dirname(report_path),
                                      "*recognized.json"))[0]
    rec = json.load(open(rec_path, encoding="utf-8"))

    # 用出材率反推每张板的已用面积（报告里没有逐板面积，但有件清单）
    areas = {}
    for panel in rec["panels"]:
        wkt = panel.get("outer_geometry_wkt") or ""
        if "(" not in wkt:
            continue
        body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
        ring = body.split(")")[0].lstrip("(")
        pts = [tuple(map(float, p.split()[:2])) for p in ring.split(",")
               if len(p.split()) >= 2]
        s = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            s += x1 * y2 - x2 * y1
        areas[panel.get("number") or panel.get("panel_id")] = abs(s) / 2e6

    used = []
    for sheet in report["sheets"]:
        used.append((sum(areas.get(n, 0.0) for n in sheet["parts"]),
                     len(sheet["parts"]), sheet["index"]))
    used.sort()

    print(f"\n== {os.path.basename(report_path)}  "
          f"{report['total_sheets']} 张 / 出材率 {report['yield_rate']}%  "
          f"单板 {sheet_area:.2f} m²")
    print(f"   最低 5 张填充: "
          + ", ".join(f"{u/sheet_area*100:.1f}%" for u, _, _ in used[:5]))
    print(f"   目标 {report['total_sheets']-1} 张需要平均填充 "
          f"{sum(u for u, _, _ in used)/((report['total_sheets']-1)*sheet_area)*100:.2f}%")

    print(f"   把 k 张最低填充板重排进 k-1 张所需填充（上限 {ACHIEVABLE_FILL*100:.0f}% 才算可行）：")
    best_k = None
    for k in range(2, min(21, len(used) + 1)):
        parts_area = sum(u for u, _, _ in used[:k])
        need = parts_area / ((k - 1) * sheet_area)
        mark = ""
        if need <= ACHIEVABLE_FILL:
            mark = "  <== 可行"
            if best_k is None:
                best_k = k
        if k <= 12 or mark:
            print(f"     k={k:<3} 件面积 {parts_area:6.2f} m² -> 需 {need*100:5.1f}%{mark}")
    print(f"   最小可行 k = {best_k if best_k else '无（>20 张也不够）'}")


def main():
    for path in sys.argv[1:]:
        analyse(path)


if __name__ == "__main__":
    main()
