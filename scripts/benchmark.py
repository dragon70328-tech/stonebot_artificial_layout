"""金标准 benchmark：一条命令给出识别准确率、板数、出材率、耗时与一致性。

设计要点（对应 2026-09-23 的冷启动结论）：

- 输入只来自 `tests/fixtures/dxf/` 或脚本内即时生成的合成图纸，**不读
  `output/`**——那个目录随时可以被清理；
- 期望值写在 `tests/fixtures/benchmark_cases.json` 里，不在代码里写死；
- 每个读取用例跑两遍并比较结果摘要 digest，锁定确定性；
- 未提供的 fixture 记为 `skipped`（CI 上不失败），本地可用 `--strict` 强制；
- 任一用例失败即返回非零退出码，可直接作为 CI 回归门。

用法：

    python -X utf8 scripts/benchmark.py
    python -X utf8 scripts/benchmark.py --json benchmark_report.json
    python -X utf8 scripts/benchmark.py --include-slow --strict
    python -X utf8 scripts/benchmark.py --names cgr45_read,synthetic_nested_polyline_holes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.drawing_profile import (  # noqa: E402
    DrawingProfile,
    load_profile,
    read_dxf_with_profile,
)
from src.units import UnitSystem  # noqa: E402


REGISTRY_PATH = PROJECT_ROOT / "tests" / "fixtures" / "benchmark_cases.json"
DEFAULT_JSON = PROJECT_ROOT / "benchmark_report.json"


# --------------------------------------------------------------------- helpers
def load_registry(path: Path | str = REGISTRY_PATH) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data.get("cases", []))


def recognized_digest(parts: list[dict[str, Any]]) -> str:
    """结果摘要：编号 + 面积 + 孔洞数，用于确定性比较。"""
    payload = [
        {
            "number": part.get("original_number"),
            "area": round(float(part.get("area") or 0.0), 3),
            "holes": len(part.get("holes") or []),
        }
        for part in parts
    ]
    payload.sort(key=lambda item: (str(item["number"]), item["area"], item["holes"]))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def summarize_parts(parts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "panels": len(parts),
        "holes": sum(len(part.get("holes") or []) for part in parts),
        "numbers": sum(1 for part in parts if part.get("original_number")),
    }


def display_path(path: Path) -> str:
    """项目内相对路径；临时目录（合成用例）退回绝对路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def resolve_case_profile(case: dict[str, Any]) -> DrawingProfile:
    """用例可以引用 drawing_profiles/<name>.json，也可以内联画像。"""
    if case.get("profile_dict") is not None:
        return DrawingProfile.from_dict(case["profile_dict"])
    name = case["profile"]
    return load_profile(PROJECT_ROOT / "drawing_profiles" / f"{name}.json")


def _read_once(dxf_path: Path, profile: DrawingProfile):
    started = time.perf_counter()
    parts, _doc = read_dxf_with_profile(dxf_path, profile)
    return parts, time.perf_counter() - started


# --------------------------------------------------------------------- runners
def run_read_case(case: dict[str, Any], dxf_path: Path) -> dict[str, Any]:
    profile = resolve_case_profile(case)
    parts, elapsed = _read_once(dxf_path, profile)
    actual = summarize_parts(parts)
    digest_first = recognized_digest(parts)

    expected = case.get("expected") or {}
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }

    deterministic = True
    digest_second = digest_first
    if case.get("check_determinism", True):
        parts_again, _ = _read_once(dxf_path, profile)
        digest_second = recognized_digest(parts_again)
        deterministic = digest_second == digest_first

    return {
        "kind": "read",
        "input": display_path(dxf_path),
        "actual": actual,
        "expected": expected,
        "mismatches": mismatches,
        "deterministic": deterministic,
        "digest": digest_first,
        "elapsed_s": round(elapsed, 2),
        "passed": not mismatches and deterministic,
    }


def run_nesting_case(case: dict[str, Any], dxf_path: Path) -> dict[str, Any]:
    import main as app
    from src.constraints import PROFILES

    name = case.get("nesting_profile", "quick")
    nesting_profile = PROFILES[name]
    width, height = case["sheet"]
    thickness = float(case.get("thickness", 20.0))
    # sheet 是标称大板尺寸；让尺与刀缝是独立工艺参数，不能并入 sheet。
    oversize = case.get("sheet_oversize")
    sheet_oversize = (
        (float(oversize[0]), float(oversize[1])) if oversize else None
    )
    kerf_mm = float(case.get("kerf_mm", 0.0))

    started = time.perf_counter()
    outcome = app.run(
        str(dxf_path),
        float(width),
        float(height),
        thickness,
        unit=UnitSystem.METRIC,
        trials=int(case.get("trials", 1)),
        seed=int(case.get("seed", 0)),
        budget=float(case.get("budget", 0.0)),
        skip_unnumbered=True,
        layers=None,
        exclude_layers=None,
        profile=nesting_profile,
        confirm_sheet_count=False,
        report_only=True,
        quick=bool(case.get("quick", True)),
        kerf_mm=kerf_mm,
        sheet_oversize=sheet_oversize,
        auto_review=False,
        cold_start=True,
    )
    elapsed = time.perf_counter() - started

    outcome = outcome or {}
    actual = {
        "total_sheets": outcome.get("total_sheets"),
        "yield_rate": outcome.get("yield_rate"),
        "layout_source": outcome.get("layout_source"),
        "sheet": [width, height],
        "kerf_mm": kerf_mm,
        "sheet_oversize": list(sheet_oversize) if sheet_oversize else None,
    }
    limits = case.get("limits") or {}
    violations: list[str] = []
    if limits.get("max_sheets") is not None and (
        actual["total_sheets"] is None
        or actual["total_sheets"] > limits["max_sheets"]
    ):
        violations.append(
            f"total_sheets {actual['total_sheets']} > max {limits['max_sheets']}"
        )
    if limits.get("min_yield") is not None and (
        actual["yield_rate"] is None or actual["yield_rate"] < limits["min_yield"]
    ):
        violations.append(
            f"yield_rate {actual['yield_rate']} < min {limits['min_yield']}"
        )
    if limits.get("max_seconds") is not None and elapsed > limits["max_seconds"]:
        violations.append(
            f"elapsed {elapsed:.1f}s > max {limits['max_seconds']}s"
        )
    if actual["layout_source"] not in (None, "algorithm"):
        violations.append(
            f"layout_source {actual['layout_source']} 不是算法排板"
        )

    return {
        "kind": "nesting",
        "input": display_path(dxf_path),
        "actual": actual,
        "limits": limits,
        "violations": violations,
        "elapsed_s": round(elapsed, 2),
        "passed": not violations,
    }


# ------------------------------------------------------------------- synthetic
def _write_dxf(path: Path, callback) -> None:
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    callback(doc.modelspace())
    doc.saveas(path)


def build_synthetic_cases(tmp_dir: Path) -> list[dict[str, Any]]:
    """不依赖任何外部文件的用例，保证 CI 在无 fixture 时也有回归门。"""

    nested = tmp_dir / "synthetic_nested_polyline_holes.dxf"

    def _nested(msp):
        msp.add_lwpolyline(
            [(0, 0), (400, 0), (400, 300), (0, 300)],
            close=True,
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_lwpolyline(
            [(100, 100), (200, 100), (200, 180), (100, 180)],
            close=True,
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_circle((320, 60), radius=20, dxfattribs={"layer": "PANEL"})

    _write_dxf(nested, _nested)

    renamed = tmp_dir / "synthetic_layer_rename.dxf"

    def _renamed(doc_msp):
        for index, x in enumerate((0, 500)):
            doc_msp.add_lwpolyline(
                [(x, 0), (x + 400, 0), (x + 400, 300), (x, 300)],
                close=True,
                dxfattribs={"layer": "PANEL"},
            )
            doc_msp.add_circle(
                (x + 200, 150), radius=30, dxfattribs={"layer": "PANEL"}
            )
            text = doc_msp.add_text(
                f"PANEL-{index + 1}", dxfattribs={"layer": "NUM"}
            )
            text.set_placement((x + 200, 150))

    _write_dxf(renamed, _renamed)

    nested_profile = {
        "name": "synthetic_nested_holes",
        "version": "1.0.0",
        "panel_layer": "PANEL",
        "use_hatch": False,
        "number_layers": [],
        "build_hierarchy": True,
        "exclude_entity_types": [],
        "exclude_linetypes": [],
    }
    renamed_profile = {
        "name": "synthetic_layer_hints_wrong",
        "version": "1.0.0",
        "panel_layers": ["OLD_PANEL_LAYER"],
        "hole_layers": ["OLD_HOLE_LAYER"],
        "number_layers": ["OLD_LABELS_LAYER"],
        "label_pattern": r"^PANEL-\d+$",
        "use_hatch": False,
        "build_hierarchy": True,
        "exclude_entity_types": [],
        "exclude_linetypes": [],
        "small_area_threshold": 100.0,
    }
    return [
        {
            "name": "synthetic_nested_polyline_holes",
            "kind": "read",
            "path": nested,
            "profile_dict": nested_profile,
            "expected": {"panels": 1, "holes": 2, "numbers": 0},
            "notes": "CGR45 153 孔丢失的最小复现：多段线孔 + 圆孔都必须记录",
        },
        {
            "name": "synthetic_layer_rename",
            "kind": "read",
            "path": renamed,
            "profile_dict": renamed_profile,
            "expected": {"panels": 2, "holes": 2, "numbers": 2},
            "notes": "图层扰动：图层名全部改名，几何 + 文本仍是主证据",
        },
    ]


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stonebot 金标准 benchmark")
    parser.add_argument("--json", type=Path, default=None, help="报告输出路径")
    parser.add_argument("--names", type=str, default=None, help="逗号分隔的用例名")
    parser.add_argument("--include-slow", action="store_true", help="包含 slow 用例")
    parser.add_argument("--strict", action="store_true", help="skipped 用例视为失败")
    args = parser.parse_args(argv)

    wanted = {name.strip() for name in (args.names or "").split(",") if name.strip()}
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="stonebot_benchmark_") as tmp:
        tmp_dir = Path(tmp)

        for case in build_synthetic_cases(tmp_dir):
            if wanted and case["name"] not in wanted:
                continue
            try:
                result = run_read_case(case, case["path"])
            except Exception as exc:  # noqa: BLE001 - 报告而不是崩溃
                result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            result.update(
                {
                    "name": case["name"],
                    "kind": case["kind"],
                    "notes": case.get("notes", ""),
                }
            )
            result.setdefault("status", "passed" if result.get("passed") else "failed")
            results.append(result)

        for case in load_registry():
            if wanted and case["name"] not in wanted:
                continue
            if case.get("slow") and not args.include_slow:
                results.append(
                    {
                        "name": case["name"],
                        "status": "skipped",
                        "passed": True,
                        "reason": "slow 用例，需 --include-slow",
                    }
                )
                continue
            dxf_path = PROJECT_ROOT / case["input"]
            if not dxf_path.exists():
                results.append(
                    {
                        "name": case["name"],
                        "status": "skipped",
                        "passed": True,
                        "reason": f"缺少样本 {case['input']}",
                    }
                )
                continue
            try:
                if case["kind"] == "read":
                    result = run_read_case(case, dxf_path)
                else:
                    result = run_nesting_case(case, dxf_path)
            except Exception as exc:  # noqa: BLE001
                result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            result.update(
                {
                    "name": case["name"],
                    "kind": case["kind"],
                    "notes": case.get("notes", ""),
                }
            )
            result.setdefault("status", "passed" if result.get("passed") else "failed")
            results.append(result)

    failed = [item for item in results if item["status"] == "failed"]
    skipped = [item for item in results if item["status"] == "skipped"]
    report = {
        "schema_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "passed": sum(1 for item in results if item["status"] == "passed"),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "cases": results,
    }

    for item in results:
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[item["status"]]
        detail = ""
        if item["status"] == "failed":
            detail = f" {item.get('mismatches') or item.get('violations') or item.get('error')}"
        elif item.get("kind") == "read":
            detail = f" {item.get('actual')} ({item.get('elapsed_s')}s)"
        elif item.get("kind") == "nesting":
            detail = f" {item.get('actual')} ({item.get('elapsed_s')}s)"
        print(f"[{mark}] {item['name']}{detail}")

    print(
        f"\n合计 {report['summary']['total']} 个用例："
        f"通过 {report['summary']['passed']}，"
        f"失败 {report['summary']['failed']}，"
        f"跳过 {report['summary']['skipped']}"
    )

    json_path = args.json
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"报告: {json_path}")

    if failed or (args.strict and skipped):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
