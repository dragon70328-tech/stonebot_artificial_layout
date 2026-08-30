import time

import pytest

from src.input_guard import (
    DXFInputError,
    DXFInputLimits,
    check_dxf_file,
    guarded_read,
)


def test_check_dxf_file_rejects_non_dxf(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("not a dxf", encoding="utf-8")

    violations = check_dxf_file(path, DXFInputLimits())

    assert any("extension" in item for item in violations)


def test_check_dxf_file_rejects_oversized_file(tmp_path):
    path = tmp_path / "big.dxf"
    path.write_bytes(b"0" * 128)

    violations = check_dxf_file(path, DXFInputLimits(max_bytes=64))

    assert any("size" in item for item in violations)


def test_guarded_read_rejects_missing_file():
    with pytest.raises(DXFInputError):
        guarded_read("missing.dxf", lambda path: ([], None), DXFInputLimits())


def test_guarded_read_rejects_too_many_entities(tmp_path):
    class FakeDoc:
        def modelspace(self):
            return list(range(3))

    def read(path):
        return [], FakeDoc()

    with pytest.raises(DXFInputError):
        guarded_read(
            tmp_path / "ok.dxf",
            read,
            DXFInputLimits(max_entities=2),
        )


def test_guarded_read_rejects_slow_parse(tmp_path):
    path = tmp_path / "ok.dxf"
    path.write_text("", encoding="utf-8")

    def read(filepath):
        time.sleep(0.02)
        return [], None

    with pytest.raises(DXFInputError):
        guarded_read(path, read, DXFInputLimits(max_parse_seconds=0.01))
