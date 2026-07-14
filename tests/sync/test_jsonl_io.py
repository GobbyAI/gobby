import threading
from pathlib import Path

import pytest

from gobby.sync import jsonl_io
from gobby.sync.jsonl_io import export_file_lock

pytestmark = pytest.mark.unit


def test_export_file_lock_serializes_concurrent_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data.jsonl"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted_lock = threading.Event()
    second_entered = threading.Event()
    assert jsonl_io.fcntl is not None
    real_flock = jsonl_io.fcntl.flock

    def tracked_flock(fd: int, operation: int) -> None:
        if threading.current_thread().name == "second" and operation == jsonl_io.fcntl.LOCK_EX:
            second_attempted_lock.set()
        real_flock(fd, operation)

    monkeypatch.setattr(jsonl_io.fcntl, "flock", tracked_flock)

    def first_writer() -> None:
        with export_file_lock(target):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_writer() -> None:
        with export_file_lock(target):
            second_entered.set()

    first = threading.Thread(target=first_writer, name="first")
    second = threading.Thread(target=second_writer, name="second")
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert second_attempted_lock.wait(timeout=2)
    assert not second_entered.is_set()
    release_first.set()

    for thread in (first, second):
        thread.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
