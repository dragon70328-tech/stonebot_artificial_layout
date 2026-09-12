"""Deterministic tool executor.

The Worker is the only place allowed to call registered Python handlers. It
validates the tool against the stage gate and its schema, runs it with a
timeout and retry budget, and returns a structured result without exposing
exceptions to the orchestrator.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from src.ai_planning import ToolCall, validate_tool_call
from src.tool_schemas import get_tool_spec
from src.workflow_session import WorkflowStage


Handler = Callable[[dict[str, Any]], Any]


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "attempts": self.attempts,
        }


class Worker:
    """Single-process executor with timeout, retry, and cooperative cancel."""

    def __init__(
        self,
        handlers: dict[str, Handler] | None = None,
        *,
        default_timeout_s: float = 60.0,
        max_attempts: int = 1,
    ) -> None:
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.default_timeout_s = default_timeout_s
        self.max_attempts = max_attempts
        self._cancel_event = threading.Event()

    def register(self, tool: str, handler: Handler) -> None:
        self.handlers[tool] = handler

    def cancel(self) -> None:
        self._cancel_event.set()

    def reset(self) -> None:
        self._cancel_event.clear()

    def execute(
        self,
        call: ToolCall,
        *,
        stage: WorkflowStage | str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        started = time.time()
        spec = get_tool_spec(call.tool)
        if spec is None:
            return ToolResult(
                tool=call.tool,
                ok=False,
                error=f"unknown tool: {call.tool}",
            )

        if stage is not None:
            gate = validate_tool_call(stage, call)
            if not gate.valid:
                return ToolResult(
                    tool=call.tool,
                    ok=False,
                    error="; ".join(gate.errors),
                )

        schema_errors = spec.validate_arguments(call)
        if schema_errors:
            return ToolResult(
                tool=call.tool,
                ok=False,
                error="; ".join(schema_errors),
            )

        handler = self.handlers.get(call.tool)
        if handler is None:
            return ToolResult(
                tool=call.tool,
                ok=False,
                error=f"no handler registered for tool {call.tool}",
            )

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self._cancel_event.is_set():
                return ToolResult(
                    tool=call.tool,
                    ok=False,
                    error="cancelled before execution",
                    elapsed_seconds=time.time() - started,
                    attempts=attempt,
                )
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(handler, call.arguments)
                    output = future.result(timeout=timeout_s or self.default_timeout_s)
                return ToolResult(
                    tool=call.tool,
                    ok=True,
                    output=output,
                    elapsed_seconds=time.time() - started,
                    attempts=attempt,
                )
            except FutureTimeout:
                last_error = (
                    f"timeout after {timeout_s or self.default_timeout_s:.1f}s"
                )
            except Exception as exc:  # noqa: BLE001 - task boundary
                last_error = str(exc)
        return ToolResult(
            tool=call.tool,
            ok=False,
            error=last_error or "execution failed",
            elapsed_seconds=time.time() - started,
            attempts=self.max_attempts,
        )
