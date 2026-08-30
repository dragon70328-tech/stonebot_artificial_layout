import time

from src.task_queue import InMemoryTaskQueue, Task, TaskStatus


def test_task_queue_runs_fifo_and_marks_succeeded():
    queue = InMemoryTaskQueue()
    queue.enqueue(Task(task_type="double", payload={"value": 2}))
    queue.enqueue(Task(task_type="double", payload={"value": 3}))

    finished = queue.run_all({"double": lambda payload: payload["value"] * 2})

    assert [task.result for task in finished] == [4, 6]
    assert all(task.status == TaskStatus.SUCCEEDED for task in finished)
    assert finished[0].attempts == 1
    assert len(finished[0].audit_log) == 3


def test_task_queue_marks_missing_handler_failed():
    queue = InMemoryTaskQueue()
    queue.enqueue(Task(task_type="unknown", payload={}))

    finished = queue.run_all({})

    assert finished[0].status == TaskStatus.FAILED
    assert "no handler" in finished[0].error


def test_task_queue_marks_handler_exception_failed():
    queue = InMemoryTaskQueue()

    def boom(payload):
        raise RuntimeError("bad")

    queue.enqueue(Task(task_type="boom", payload={}))
    finished = queue.run_all({"boom": boom})

    assert finished[0].status == TaskStatus.FAILED
    assert finished[0].error == "bad"


def test_task_queue_marks_timeout():
    queue = InMemoryTaskQueue()
    queue.enqueue(Task(task_type="slow", payload={}, timeout_s=0.01))

    finished = queue.run_all({"slow": lambda payload: time.sleep(1)})

    assert finished[0].status == TaskStatus.TIMED_OUT
