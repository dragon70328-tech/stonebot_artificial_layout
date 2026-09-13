"""In-process pub/sub for worker progress events.

SSE endpoints subscribe to this broker and receive structured task/job
updates. It intentionally keeps events local to one process; a future
multi-worker deployment should replace this with Redis Streams or a message
broker while preserving the same event shape.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any


def format_sse(event: dict[str, Any]) -> str:
    """Render one broker event as an SSE frame."""
    name = str(event.get("event", "message"))
    data = json.dumps(event.get("data", {}), ensure_ascii=False)
    return f"event: {name}\ndata: {data}\n\n"


class TaskEventBroker:
    """Small thread-safe fan-out queue for task/job progress events."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self.max_queue_size = max_queue_size
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.RLock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=self.max_queue_size
        )
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass
