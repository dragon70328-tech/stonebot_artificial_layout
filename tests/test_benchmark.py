"""benchmark 脚本自身的契约测试。

脚本是回归门（CI 会跑），所以它自己必须被测：注册表结构、用例名唯一、
以及"不依赖任何 fixture 也能跑通"的快车道子集。
"""

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "benchmark",
        PROJECT_ROOT / "scripts" / "benchmark.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_loads_and_names_are_unique():
    cases = _load_benchmark().load_registry()
    names = [case["name"] for case in cases]

    assert names, "注册表不能为空"
    assert len(names) == len(set(names))
    for case in cases:
        assert case["kind"] in {"read", "nesting"}
        assert case.get("expected") or case.get("limits")
        assert case.get("notes"), f"{case['name']} 必须说明期望值来源"


def test_cgr45_read_case_declares_profile_expectations():
    cases = {case["name"]: case for case in _load_benchmark().load_registry()}
    case = cases["cgr45_read"]

    assert case["expected"] == {"panels": 203, "holes": 306, "numbers": 203}
    assert case["input"].startswith("tests/fixtures/dxf/")


def test_synthetic_fast_subset_passes_without_fixtures():
    module = _load_benchmark()

    exit_code = module.main(
        ["--names", "synthetic_nested_polyline_holes,synthetic_layer_rename"]
    )

    assert exit_code == 0
