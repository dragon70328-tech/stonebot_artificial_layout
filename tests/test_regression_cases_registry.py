import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_check_output_cases():
    spec = importlib.util.spec_from_file_location(
        "check_output_cases",
        PROJECT_ROOT / "scripts" / "check_output_cases.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regression_case_registry_has_required_names():
    cases = _load_check_output_cases().CASES
    names = {case["name"] for case in cases}

    assert {
        "canada3l_bridge",
        "cgr45_bridge",
        "blockd_bridge",
        "mercury_bridge",
        "outlets1_waterjet",
        "outlets2_waterjet",
    } <= names


def test_regression_case_names_are_unique():
    cases = _load_check_output_cases().CASES
    names = [case["name"] for case in cases]

    assert len(names) == len(set(names))
