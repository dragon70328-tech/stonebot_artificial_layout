"""Minimal task-queue boundary for future worker execution.

Tasks are plain structured payloads and the queue only calls registered
handlers. This keeps the current CLI synchronous while providing the contract
needed to move read/number/nest/postprocess/write into separate workers later.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    DEGRADED = "degraded"


@dataclass
class Task:
    task_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    max_attempts: int = 1
    timeout_s: float = 60.0
    result: Any = None
    error: str | None = None
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def _audit(self, event: str, **extra: Any) -> None:
        self.audit_log.append(
            {"at": time.time(), "event": event, **extra}
        )

    def mark_started(self) -> None:
        self.status = TaskStatus.RUNNING
        self.attempts += 1
        self.started_at = time.time()
        self._audit("started", attempt=self.attempts)

    def mark_succeeded(self, result: Any) -> None:
        self.status = TaskStatus.SUCCEEDED
        self.result = result
        self.finished_at = time.time()
        self._audit("succeeded")

    def mark_failed(self, error: str, *, degraded: bool = False) -> None:
        self.status = TaskStatus.DEGRADED if degraded else TaskStatus.FAILED
        self.error = error
        self.finished_at = time.time()
        self._audit(self.status.value, error=error)

    def mark_timed_out(self) -> None:
        self.status = TaskStatus.TIMED_OUT
        self.error = f"timeout after {self.timeout_s:.1f}s"
        self.finished_at = time.time()
        self._audit("timed_out", timeout_s=self.timeout_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "timeout_s": self.timeout_s,
            "result": self.result,
            "error": self.error,
            "audit_log": self.audit_log,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        task = cls(
            task_type=data["task_type"],
            payload=dict(data.get("payload") or {}),
            task_id=data["task_id"],
            max_attempts=int(data.get("max_attempts", 1)),
            timeout_s=float(data.get("timeout_s", 60.0)),
        )
        task.status = TaskStatus(data["status"])
        task.attempts = int(data.get("attempts", 0))
        task.result = data.get("result")
        task.error = data.get("error")
        task.audit_log = list(data.get("audit_log") or [])
        task.created_at = float(data["created_at"])
        task.started_at = data.get("started_at")
        task.finished_at = data.get("finished_at")
        return task


Handler = Callable[[dict[str, Any]], Any]


class InMemoryTaskQueue:
    """Single-process FIFO queue with retry and timeout accounting."""

    def __init__(self) -> None:
        self._queue: deque[Task] = deque()
        self.finished: list[Task] = []

    def enqueue(self, task: Task) -> Task:
        self._queue.append(task)
        task._audit("enqueued")
        return task

    def has_pending(self) -> bool:
        return bool(self._queue)

    def run_all(
        self,
        handlers: dict[str, Handler],
        *,
        timeout_s: float | None = None,
    ) -> list[Task]:
        """Run queued tasks in FIFO order and return finished tasks."""
        results: list[Task] = []
        while self._queue:
            task = self._queue.popleft()
            self._run_task(task, handlers, timeout_s)
            results.append(task)
            self.finished.append(task)
        return results

    def _run_task(
        self,
        task: Task,
        handlers: dict[str, Handler],
        timeout_s: float | None,
    ) -> None:
        handler = handlers.get(task.task_type)
        if handler is None:
            task.mark_failed(f"no handler for task type {task.task_type}")
            return

        effective_timeout = timeout_s or task.timeout_s
        task.mark_started()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, task.payload)
            try:
                result = future.result(timeout=effective_timeout)
            except FutureTimeout:
                task.mark_timed_out()
                return
            except Exception as exc:  # noqa: BLE001 - task boundary
                task.mark_failed(str(exc))
                return
        task.mark_succeeded(result)


class FileTaskQueue:
    """Directory-backed FIFO queue that survives process restarts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.finished: list[Task] = []

    def _path(self, task_id: str) -> Path:
        safe_id = "".join(
            ch if ch.isalnum() or ch in "-_." else "_" for ch in task_id
        )
        return self.tasks_dir / f"{safe_id}.json"

    def _write(self, task: Task) -> None:
        self._path(task.task_id).write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_all(self) -> list[Task]:
        tasks: list[Task] = []
        for path in self.tasks_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            tasks.append(Task.from_dict(payload))
        return tasks

    def enqueue(self, task: Task) -> Task:
        task._audit("enqueued")
        self._write(task)
        return task

    def load(self, task_id: str) -> Task:
        path = self._path(task_id)
        if not path.exists():
            raise KeyError(f"task not found: {task_id}")
        return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def has_pending(self) -> bool:
        return any(task.status == TaskStatus.PENDING for task in self._load_all())

    def run_all(
        self,
        handlers: dict[str, Handler],
        *,
        timeout_s: float | None = None,
    ) -> list[Task]:
        pending = [
            task
            for task in self._load_all()
            if task.status == TaskStatus.PENDING
        ]
        pending.sort(key=lambda task: (task.created_at, task.task_id))
        results: list[Task] = []
        for task in pending:
            self._run_task(task, handlers, timeout_s)
            self._write(task)
            results.append(task)
            self.finished.append(task)
        return results

    def _run_task(
        self,
        task: Task,
        handlers: dict[str, Handler],
        timeout_s: float | None,
    ) -> None:
        handler = handlers.get(task.task_type)
        if handler is None:
            task.mark_failed(f"no handler for task type {task.task_type}")
            return

        effective_timeout = timeout_s or task.timeout_s
        task.mark_started()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, task.payload)
            try:
                result = future.result(timeout=effective_timeout)
            except FutureTimeout:
                task.mark_timed_out()
                return
            except Exception as exc:  # noqa: BLE001 - task boundary
                task.mark_failed(str(exc))
                return
        task.mark_succeeded(result)
