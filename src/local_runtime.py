"""Local-first Runtime paths and single-instance coordination.

The existing CLI and API paths keep their historical defaults.  This module is
the installation boundary for the distributable desktop Runtime: data, jobs,
sessions, profiles and logs live under one per-user root.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO


LOCAL_RUNTIME_ROOT_ENV = "STONEBOT_LOCAL_ROOT"
DEFAULT_RUNTIME_NAME = "StoneBot"


class RuntimeAlreadyRunning(RuntimeError):
    """Raised when another Runtime process owns the per-user lock."""


class RuntimeLockUnavailable(RuntimeError):
    """Raised when the operating-system file lock cannot be used."""


def default_runtime_root() -> Path:
    """Return the per-user default application root."""
    override = os.environ.get(LOCAL_RUNTIME_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / DEFAULT_RUNTIME_NAME
        return Path.home() / "AppData" / "Local" / DEFAULT_RUNTIME_NAME

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / DEFAULT_RUNTIME_NAME.lower()
    return Path.home() / ".local" / "share" / DEFAULT_RUNTIME_NAME.lower()


@dataclass(frozen=True)
class LocalRuntimeLayout:
    """Filesystem layout owned by one local StoneBot installation."""

    root: Path

    @classmethod
    def resolve(
        cls,
        root: str | Path | None = None,
    ) -> "LocalRuntimeLayout":
        selected = (
            Path(root).expanduser().resolve()
            if root is not None
            else default_runtime_root()
        )
        return cls(root=selected)

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def instance_lock_path(self) -> Path:
        return self.runtime_dir / "runtime.lock"

    def ensure(self) -> "LocalRuntimeLayout":
        for directory in (
            self.runtime_dir,
            self.data_dir,
            self.jobs_dir,
            self.sessions_dir,
            self.artifacts_dir,
            self.profiles_dir,
            self.plugins_dir,
            self.logs_dir,
            self.config_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def describe(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "runtime": str(self.runtime_dir),
            "data": str(self.data_dir),
            "jobs": str(self.jobs_dir),
            "sessions": str(self.sessions_dir),
            "artifacts": str(self.artifacts_dir),
            "profiles": str(self.profiles_dir),
            "plugins": str(self.plugins_dir),
            "logs": str(self.logs_dir),
            "config": str(self.config_dir),
        }


class RuntimeInstanceLock(AbstractContextManager["RuntimeInstanceLock"]):
    """Cross-process exclusive lock for one per-user Runtime instance."""

    def __init__(
        self,
        lock_path: str | Path,
        *,
        timeout_seconds: float = 0.0,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "RuntimeInstanceLock":
        if self._handle is not None:
            return self

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock(handle)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeAlreadyRunning(
                        f"StoneBot Runtime is already running: {self.lock_path}"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "lock_path": str(self.lock_path),
                },
                ensure_ascii=False,
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "RuntimeInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    @staticmethod
    def _try_lock(handle: IO[str]) -> None:
        if os.name == "nt":
            try:
                import msvcrt
            except ImportError as exc:  # pragma: no cover - Windows only
                raise RuntimeLockUnavailable("msvcrt is unavailable") from exc
            handle.seek(0)
            if handle.read(1) == "":
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - POSIX only
            raise RuntimeLockUnavailable("fcntl is unavailable") from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_runtime_instance(
    layout: LocalRuntimeLayout,
    *,
    timeout_seconds: float = 0.0,
) -> RuntimeInstanceLock:
    layout.ensure()
    return RuntimeInstanceLock(
        layout.instance_lock_path,
        timeout_seconds=timeout_seconds,
    ).acquire()
