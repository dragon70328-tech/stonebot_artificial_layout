import main as app


def test_check_only_exit_code_is_zero_for_warnings_only():
    outcome = {
        "geometry_errors": [],
        "postprocess_warnings": [
            {"type": "through_cut_constrained", "sheet": 1, "edges": []}
        ],
        "check_path": "check.json",
    }
    assert app.check_only_exit_code(outcome) == 0


def test_check_only_exit_code_is_nonzero_for_geometry_errors():
    outcome = {
        "geometry_errors": ["Sheet 1: A 超出大板边界"],
        "postprocess_warnings": [],
        "check_path": "check.json",
    }
    assert app.check_only_exit_code(outcome) == 2


def test_check_only_exit_code_is_zero_for_empty_outcome():
    assert app.check_only_exit_code(None) == 0
    assert app.check_only_exit_code({}) == 0
