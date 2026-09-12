"""Minimal task-queue boundary for future worker execution.

Tasks are plain structured payloads and the queue only calls registered
handlers. This keeps the current CLI synchronous while providing the contract
needed to move read/number/nest/postprocess/write into separate workers later.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
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
    DEAD_LETTER = "dead_letter"


@dataclass
class Task:
    task_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    max_attempts: int = 1
    timeout_s: float = 60.0
    retry_delay_s: float = 0.0
    sequence: int = 0
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

    def mark_dead_letter(self, error: str) -> None:
        self.status = TaskStatus.DEAD_LETTER
        self.error = error
        self.finished_at = time.time()
        self._audit("dead_letter", error=error)

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
            "retry_delay_s": self.retry_delay_s,
            "sequence": self.sequence,
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
            retry_delay_s=float(data.get("retry_delay_s", 0.0)),
        )
        task.sequence = int(data.get("sequence", 0))
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


def _run_task_with_retry(
    task: Task,
    handlers: dict[str, Handler],
    timeout_s: float | None,
) -> None:
    handler = handlers.get(task.task_type)
    if handler is None:
        task.mark_failed(f"no handler for task type {task.task_type}")
        return

    effective_timeout = timeout_s or task.timeout_s
    while task.attempts < task.max_attempts:
        task.mark_started()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(handler, task.payload)
                result = future.result(timeout=effective_timeout)
        except FutureTimeout:
            task.mark_timed_out()
            return
        except Exception as exc:  # noqa: BLE001 - task boundary
            last_error = str(exc)
        else:
            task.mark_succeeded(result)
            return

        if task.attempts < task.max_attempts:
            task.status = TaskStatus.PENDING
            task.error = None
            task.finished_at = None
            task._audit(
                "retry",
                attempt=task.attempts + 1,
                error=last_error,
            )
            if task.retry_delay_s:
                time.sleep(task.retry_delay_s * task.attempts)
        else:
            if task.max_attempts > 1:
                task.mark_dead_letter(last_error)
            else:
                task.mark_failed(last_error)


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
        _run_task_with_retry(task, handlers, timeout_s)


class SqliteTaskQueue:
    """SQLite-backed FIFO queue for the SaaS database boundary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.root / "tasks.sqlite3"),
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.finished: list[Task] = []
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    timeout_s REAL NOT NULL,
                    retry_delay_s REAL NOT NULL,
                    sequence INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    audit_log_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                )
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_status_sequence
                ON tasks(status, sequence, created_at)
                """
            )

    def _json_or_none(self, value: Any) -> str | None:
        return json.dumps(value, ensure_ascii=False) if value is not None else None

    def _json_loads(self, value: str | None) -> Any:
        return json.loads(value) if value else None

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        task = Task(
            task_type=row["task_type"],
            payload=json.loads(row["payload_json"]),
            task_id=row["task_id"],
            max_attempts=int(row["max_attempts"]),
            timeout_s=float(row["timeout_s"]),
            retry_delay_s=float(row["retry_delay_s"]),
        )
        task.status = TaskStatus(row["status"])
        task.attempts = int(row["attempts"])
        task.sequence = int(row["sequence"])
        task.result = self._json_loads(row["result_json"])
        task.error = row["error"]
        task.audit_log = list(self._json_loads(row["audit_log_json"]) or [])
        task.created_at = float(row["created_at"])
        task.started_at = row["started_at"]
        task.finished_at = row["finished_at"]
        return task

    def _write(self, task: Task) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tasks (
                    task_id, task_type, payload_json, status, attempts,
                    max_attempts, timeout_s, retry_delay_s, sequence,
                    result_json, error, audit_log_json, created_at,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_type=excluded.task_type,
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    attempts=excluded.attempts,
                    max_attempts=excluded.max_attempts,
                    timeout_s=excluded.timeout_s,
                    retry_delay_s=excluded.retry_delay_s,
                    sequence=excluded.sequence,
                    result_json=excluded.result_json,
                    error=excluded.error,
                    audit_log_json=excluded.audit_log_json,
                    created_at=excluded.created_at,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at
                """,
                (
                    task.task_id,
                    task.task_type,
                    json.dumps(task.payload, ensure_ascii=False),
                    task.status.value,
                    task.attempts,
                    task.max_attempts,
                    task.timeout_s,
                    task.retry_delay_s,
                    task.sequence,
                    self._json_or_none(task.result),
                    task.error,
                    json.dumps(task.audit_log, ensure_ascii=False),
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                ),
            )

    def enqueue(self, task: Task) -> Task:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM tasks"
            ).fetchone()
            task.sequence = int(row["max_sequence"]) + 1
            task._audit("enqueued")
            self._write(task)
            return task

    def load(self, task_id: str) -> Task:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task not found: {task_id}")
            return self._task_from_row(row)

    def has_pending(self) -> bool:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT 1 FROM tasks WHERE status = ? LIMIT 1
                """,
                (TaskStatus.PENDING.value,),
            ).fetchone()
            return row is not None

    def list_tasks(self) -> list[Task]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM tasks
                ORDER BY sequence, created_at, task_id
                """
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def dead_letters(self) -> list[Task]:
        return [
            task
            for task in self.list_tasks()
            if task.status == TaskStatus.DEAD_LETTER
        ]

    def retry_task(self, task_id: str) -> Task:
        with self._lock:
            task = self.load(task_id)
            if task.status not in {
                TaskStatus.FAILED,
                TaskStatus.TIMED_OUT,
                TaskStatus.DEAD_LETTER,
            }:
                raise ValueError(
                    f"task {task_id} cannot be retried from {task.status.value}"
                )
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.error = None
            task.finished_at = None
            task.result = None
            task._audit("requeued")
            self._write(task)
            return task

    def run_all(
        self,
        handlers: dict[str, Handler],
        *,
        timeout_s: float | None = None,
    ) -> list[Task]:
        with self._lock:
            pending = [
                task
                for task in self.list_tasks()
                if task.status == TaskStatus.PENDING
            ]
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
        _run_task_with_retry(task, handlers, timeout_s)


def build_task_queue(root: str | Path) -> FileTaskQueue | SqliteTaskQueue:
    """Select the queue backend from ``STONEBOT_TASK_STORE``."""
    if os.environ.get("STONEBOT_TASK_STORE", "file").strip().lower() == "sqlite":
        return SqliteTaskQueue(root)
    return FileTaskQueue(root)


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
        existing = self._load_all()
        task.sequence = max(
            (item.sequence for item in existing),
            default=0,
        ) + 1
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

    def list_tasks(self) -> list[Task]:
        tasks = self._load_all()
        tasks.sort(
            key=lambda task: (task.sequence, task.created_at, task.task_id)
        )
        return tasks

    def dead_letters(self) -> list[Task]:
        return [
            task
            for task in self.list_tasks()
            if task.status == TaskStatus.DEAD_LETTER
        ]

    def retry_task(self, task_id: str) -> Task:
        task = self.load(task_id)
        if task.status not in {
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.DEAD_LETTER,
        }:
            raise ValueError(f"task {task_id} cannot be retried from {task.status.value}")
        task.status = TaskStatus.PENDING
        task.attempts = 0
        task.error = None
        task.finished_at = None
        task.result = None
        task._audit("requeued")
        self._write(task)
        return task

    def run_all(
        self,
        handlers: dict[str, Handler],
        *,
        timeout_s: float | None = None,
    ) -> list[Task]:
        pending = [
            task
            for task in self.list_tasks()
            if task.status == TaskStatus.PENDING
        ]
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
        _run_task_with_retry(task, handlers, timeout_s)
