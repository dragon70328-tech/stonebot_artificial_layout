import time

from src.task_queue import FileTaskQueue, InMemoryTaskQueue, Task, TaskStatus


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


def test_file_task_queue_persists_and_runs(tmp_path):
    first = FileTaskQueue(tmp_path)
    first.enqueue(Task(task_type="double", payload={"value": 2}))
    first.enqueue(Task(task_type="double", payload={"value": 3}))

    second = FileTaskQueue(tmp_path)
    assert second.has_pending() is True
    finished = second.run_all({"double": lambda payload: payload["value"] * 2})

    assert [task.result for task in finished] == [4, 6]
    assert all(task.status == TaskStatus.SUCCEEDED for task in finished)
    assert second.load(finished[0].task_id).status == TaskStatus.SUCCEEDED


def test_file_task_queue_survives_restart_after_failure(tmp_path):
    first = FileTaskQueue(tmp_path)
    task = first.enqueue(Task(task_type="boom", payload={}))

    def boom(payload):
        raise RuntimeError("bad")

    first.run_all({"boom": boom})

    recovered = FileTaskQueue(tmp_path)
    loaded = recovered.load(task.task_id)

    assert loaded.status == TaskStatus.FAILED
    assert loaded.error == "bad"
    assert recovered.has_pending() is False


def test_task_queue_retries_then_succeeds():
    calls = []

    def flaky(payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt failed")
        return payload["value"] * 2

    queue = InMemoryTaskQueue()
    queue.enqueue(
        Task(
            task_type="flaky",
            payload={"value": 5},
            max_attempts=2,
            retry_delay_s=0,
        )
    )

    finished = queue.run_all({"flaky": flaky})

    assert finished[0].result == 10
    assert finished[0].attempts == 2
    assert any(event["event"] == "retry" for event in finished[0].audit_log)


def test_file_task_queue_retries_then_persists_success(tmp_path):
    calls = []

    def flaky(payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt failed")
        return payload["value"] * 2

    queue = FileTaskQueue(tmp_path)
    task = queue.enqueue(
        Task(
            task_type="flaky",
            payload={"value": 6},
            max_attempts=2,
            retry_delay_s=0,
        )
    )

    finished = queue.run_all({"flaky": flaky})

    assert finished[0].result == 12
    assert queue.load(task.task_id).attempts == 2


def test_task_queue_marks_dead_letter_after_retries():
    def always_boom(payload):
        raise RuntimeError("still broken")

    queue = InMemoryTaskQueue()
    task = queue.enqueue(
        Task(
            task_type="always_boom",
            payload={},
            max_attempts=2,
            retry_delay_s=0,
        )
    )

    finished = queue.run_all({"always_boom": always_boom})

    assert task.status == TaskStatus.DEAD_LETTER
    assert finished[0].status == TaskStatus.DEAD_LETTER
    assert any(event["event"] == "dead_letter" for event in task.audit_log)
