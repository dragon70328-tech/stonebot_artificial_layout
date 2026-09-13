"""Standalone worker process for Stonebot nesting jobs.

Run this process separately from the FastAPI API when long nesting jobs should
not compete with web request handling. Both processes share ``STONEBOT_DATA_ROOT``
and must use the SQLite task store.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    root = Path(
        os.environ.get(
            "STONEBOT_DATA_ROOT",
            str(project_root / "output" / "api_jobs"),
        )
    )
    worker_id = os.environ.get("STONEBOT_WORKER_ID") or "worker-1"
    os.environ.setdefault("STONEBOT_TASK_STORE", "sqlite")
    os.environ.setdefault("STONEBOT_WORKER_ENABLED", "1")

    from src.http_api import JobManager

    manager = JobManager(root)
    print(f"stonebot api worker started: {worker_id} root={root}", flush=True)
    manager._ensure_worker()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
