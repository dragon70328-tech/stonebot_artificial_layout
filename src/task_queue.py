"""Minimal task-queue boundary for future worker execution.

Tasks are plain structured payloads and the queue only calls registered
handlers. This keeps the current CLI synchronous while providing the contract
needed to move read/number/nest/postprocess/write into separate workers later.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
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
