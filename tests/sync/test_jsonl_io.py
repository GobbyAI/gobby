import fcntl
import os
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


def test_export_file_lock_holds_unwritable_lock_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data.jsonl"
    lock_path = tmp_path / ".data.jsonl.lock"
    lock_path.touch()
    real_open = os.open

    # A sandbox that denies writes refuses read-write opens even for root.
    def write_denied_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if os.fspath(path) == str(lock_path) and flags & os.O_RDWR:
            raise PermissionError(f"write denied: {path}")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", write_denied_open)

    with (
        export_file_lock(target),
        lock_path.open("rb") as contender,
        pytest.raises(BlockingIOError),
    ):
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
