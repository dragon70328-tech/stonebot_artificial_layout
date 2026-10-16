"""已验证案例库 ↔ 本地夹具的读图回归门禁。

画像库的标准是"画像 + fingerprint + expected_counts + notes + 已验证案例"
（见 AGENTS.md「画像库积累」）。本模块让**案例库直接变成回归网**：

* 案例声明的 ``source_sha256`` 能在 ``tests/fixtures/dxf/`` 里找到时，
  用生产读图链路复算件数并断言与案例声明一致；
* 源图未入库的案例以 ``pytest.skip`` 显式暴露覆盖缺口（不静默通过）；
* 已知回归（见 ``KNOWN_REGRESSIONS``）登记为 ``xfail(strict=True)``：
  现在必须失败，修好后会 XPASS 并立刻变红，逼着修复者改回正常断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from src.cli_audit import file_sha256
from src.cli_paths import PROJECT_ROOT
from src.drawing_profile import (
    DrawingProfile,
    load_profile,
    read_dxf_with_profile,
)

CASES_DIR = PROJECT_ROOT / "drawing_profiles" / "validated_cases"
PROFILES_DIR = PROJECT_ROOT / "drawing_profiles"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "dxf"
REGISTRY_PATH = PROJECT_ROOT / "tests" / "fixtures" / "benchmark_cases.json"

# 2026-10-16 回填体检发现、尚未修复的读图回归（证据见 AGENTS.md
# 「案例库立刻暴露的三处"声明值已过期"」与 _analysis/worklog_2026-10-16.md）。
KNOWN_REGRESSIONS = {
    "fuzhou_wanxiangcheng1": (
        "图层过滤回归：143 件里有 10 件来自画像明确排除的图层"
        "（F-地材造型线 5、ST-02算量 4、现场实际间隔柱定位 1），"
        "孔洞 36→268 与之一并膨胀；应回到 133/36/133。"
    ),
}

# 水星没有入库夹具（原始图纸 11MB），用最小复现覆盖同一根因：
# 修复后本用例会 XPASS 并变红，提示把水星的原始图纸也纳入门禁。
CLOSURE_TOLERANCE_REGRESSION = (
    "闭合容差回归：画像声明 closed_tolerance=0.1，但兜闭合判定被 49da243 "
    "钳到 0.01mm，末点落在已有边 0.031mm 处的轮廓被静默丢弃"
    "（水星 15 件、1258→1243）。"
)


def _fixture_index() -> dict[str, Path]:
    """夹具按内容建索引：案例的 source_sha256 只认内容，不认文件名。"""
    return {
        file_sha256(path): path for path in sorted(FIXTURES_DIR.glob("*.dxf"))
    }


def _load_cases() -> list[tuple[str, dict]]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        cases.append((str(data.get("case_id") or path.stem), data))
    return cases


def _read_counts(path: Path, profile_name: str) -> dict[str, int]:
    profile = load_profile(PROFILES_DIR / f"{profile_name}.json")
    parts, _doc = read_dxf_with_profile(path, profile)
    return {
        "panels": len(parts),
        "holes": sum(len(part.get("holes") or []) for part in parts),
        "numbers": sum(
            1 for part in parts if part.get("original_number")
        ),
    }


def _case_params() -> list:
    params = []
    for case_id, data in _load_cases():
        marks = []
        if case_id in KNOWN_REGRESSIONS:
            marks.append(
                pytest.mark.xfail(
                    strict=True, reason=KNOWN_REGRESSIONS[case_id]
                )
            )
        params.append(pytest.param(case_id, data, marks=marks, id=case_id))
    return params


@pytest.mark.parametrize("case_id, case", _case_params())
def test_case_counts_match_library(case_id, case):
    """案例声明的件数必须能被当前代码复现。"""
    digest = case.get("source_sha256")
    fixture = _fixture_index().get(digest) if digest else None
    if fixture is None:
        reason = (
            "案例没有 source_sha256"
            if not digest
            else "源图未入库（tests/fixtures/dxf/ 里没有该 sha256）"
        )
        pytest.skip(f"{case_id}：{reason}，本案例暂不在门禁内")

    expected = case.get("expected_counts") or {}
    actual = _read_counts(fixture, str(case.get("profile_name") or case_id))
    mismatches = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in ("panels", "holes", "numbers")
        if expected.get(key) is not None and expected.get(key) != actual.get(key)
    }
    assert not mismatches, f"{case_id} 读图件数与案例声明不一致：{mismatches}"


def test_benchmark_registry_agrees_with_case_library():
    """benchmark_cases.json 与案例库对同一张源图不得给出不同期望值。"""
    index = _fixture_index()
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    library: dict[str, dict] = {}
    for path in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        digest = data.get("source_sha256")
        if digest:
            library[digest] = data

    conflicts = []
    for case in registry.get("cases", []):
        if case.get("kind") != "read":
            continue
        input_path = PROJECT_ROOT / str(case.get("input", ""))
        if not input_path.exists():
            continue
        declared = library.get(file_sha256(input_path))
        if not declared:
            continue
        for key, value in (case.get("expected") or {}).items():
            library_value = (declared.get("expected_counts") or {}).get(key)
            if library_value is not None and library_value != value:
                conflicts.append(
                    f"{case.get('name')}.{key}: 基准={value} 案例库={library_value}"
                )
    assert not conflicts, f"两处期望值不一致：{conflicts}"


@pytest.mark.xfail(strict=True, reason=CLOSURE_TOLERANCE_REGRESSION)
def test_open_loop_with_last_vertex_on_edge_is_recovered(tmp_path):
    """末点落在已有边上 0.031mm 的轮廓必须按画像声明的闭合容差兜回来。

    顶点取自水星 11DA7（F-3厨房-01，L 形 1620x2540 台面）：v7 距 v1 仅
    0.0309mm，是 CAD 导出留下的收口误差。49da243「分离闭合与清理容差」
    把这条兜闭合判定也钳到 0.01mm，于是 closed_tolerance=0.1 的画像
    静默丢掉 15 件（水星 1258→1243）。
    """
    path = tmp_path / "closure.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.modelspace().add_lwpolyline(
        [
            (44456.5980, -92078.4183),
            (44456.5980, -92722.9433),
            (46077.4355, -92722.9433),
            (46077.4355, -92078.4183),
            (45101.1230, -92078.4183),
            (45101.1230, -90182.9742),
            (44456.5980, -90182.9742),
            (44456.5980, -92722.9742),
        ],
        dxfattribs={"layer": "PANEL"},
    )
    doc.saveas(path)

    profile = DrawingProfile.from_dict(
        {
            "name": "closure_repro",
            "version": "1.0.0",
            "panel_layer": "PANEL",
            "panel_layers": ["PANEL"],
            "closed_tolerance": 0.1,
            "build_hierarchy": False,
            "number_layers": [],
        }
    )
    parts, _doc = read_dxf_with_profile(path, profile)

    assert len(parts) == 1
