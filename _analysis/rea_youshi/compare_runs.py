"""比较两次排板运行的产物是否逐件一致（确定性验收）。

用法：
    python -X utf8 _analysis/rea_youshi/compare_runs.py <run_dir_a> <run_dir_b>

比较内容：板数、出材率、每张板的零件清单、优化器诊断（不含秒数）。
"""

import glob
import json
import os
import sys


def load_report(d):
    path = glob.glob(os.path.join(d, "*report_*.json"))[0]
    return path, json.load(open(path, encoding="utf-8"))


def signature(report):
    sheets = [(s["index"], tuple(s["parts"])) for s in report["sheets"]]
    opt = {
        k: v for k, v in (report.get("parameters", {}).get("optimizer") or {}).items()
        if "seconds" not in k
    }
    return {
        "total_sheets": report["total_sheets"],
        "yield_rate": report["yield_rate"],
        "sheets": sheets,
        "optimizer": opt,
        "auto_review": report.get("auto_review"),
    }


def main():
    a, b = sys.argv[1], sys.argv[2]
    pa, ra = load_report(a)
    pb, rb = load_report(b)
    sa, sb = signature(ra), signature(rb)
    print(f"A = {pa}")
    print(f"B = {pb}")
    for key in ("total_sheets", "yield_rate", "auto_review"):
        same = sa[key] == sb[key]
        print(f"  {key:<14} {'一致' if same else '不一致'}  A={sa[key]}  B={sb[key]}")
    same_sheets = sa["sheets"] == sb["sheets"]
    print(f"  {'每板零件清单':<12} {'一致' if same_sheets else '不一致'}")
    if not same_sheets:
        diff = [(x, y) for x, y in zip(sa["sheets"], sb["sheets"]) if x != y]
        print(f"    差异板数: {len(diff)}，前 3 处: {diff[:3]}")
    for key in sorted(set(sa["optimizer"]) | set(sb["optimizer"])):
        va, vb = sa["optimizer"].get(key), sb["optimizer"].get(key)
        flag = "一致" if va == vb else "不一致"
        print(f"  optimizer.{key:<26} {flag}  A={va}  B={vb}")
    print("\n结论：" + ("两次运行完全一致（同输入同输出）"
                        if sa == sb else "两次运行存在差异"))


if __name__ == "__main__":
    main()
