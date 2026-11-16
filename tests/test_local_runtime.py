"""Local Runtime filesystem and single-instance lock tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.local_runtime import (
    LOCAL_RUNTIME_ROOT_ENV,
    LocalRuntimeLayout,
    RuntimeAlreadyRunning,
    RuntimeInstanceLock,
)


def test_layout_uses_explicit_root_and_creates_local_directories(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "StoneBot").ensure()

    assert layout.root == (tmp_path / "StoneBot").resolve()
    assert layout.jobs_dir.is_dir()
    assert layout.sessions_dir.is_dir()
    assert layout.artifacts_dir.is_dir()
    assert layout.settings_path.parent.is_dir()
    assert layout.describe()["jobs"].endswith("data\\jobs") or layout.describe()[
        "jobs"
    ].endswith("data/jobs")


def test_layout_reads_local_runtime_environment_override(tmp_path, monkeypatch):
    expected = tmp_path / "custom-runtime"
    monkeypatch.setenv(LOCAL_RUNTIME_ROOT_ENV, str(expected))

    layout = LocalRuntimeLayout.resolve()

    assert layout.root == expected.resolve()


def test_runtime_lock_blocks_a_second_process(tmp_path):
    lock_path = tmp_path / "runtime.lock"
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from src.local_runtime import RuntimeInstanceLock\n"
        "lock=RuntimeInstanceLock(Path(sys.argv[1])).acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeAlreadyRunning):
            RuntimeInstanceLock(lock_path).acquire()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    with RuntimeInstanceLock(lock_path, timeout_seconds=2.0).acquire():
        pass
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
