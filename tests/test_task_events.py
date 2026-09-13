import queue

import pytest

from src.task_events import TaskEventBroker, format_sse


def test_broker_fanout_and_unsubscribe():
    broker = TaskEventBroker()
    first = broker.subscribe()
    second = broker.subscribe()

    broker.publish({"event": "task_started", "data": {"task_id": "a"}})

    assert first.get_nowait()["event"] == "task_started"
    assert second.get_nowait()["event"] == "task_started"

    broker.unsubscribe(first)
    broker.publish({"event": "task_finished", "data": {"task_id": "a"}})

    assert second.get_nowait()["event"] == "task_finished"
    with pytest.raises(queue.Empty):
        first.get_nowait()


def test_broker_drops_oldest_when_subscriber_is_full():
    broker = TaskEventBroker(max_queue_size=1)
    subscriber = broker.subscribe()

    broker.publish({"event": "first", "data": {}})
    broker.publish({"event": "second", "data": {}})

    assert subscriber.get_nowait()["event"] == "second"


def test_format_sse_renders_event_and_data():
    frame = format_sse({"event": "task_started", "data": {"task_id": "a"}})

    assert frame == 'event: task_started\ndata: {"task_id": "a"}\n\n'
