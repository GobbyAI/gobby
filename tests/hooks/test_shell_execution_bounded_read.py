"""Script reads a privacy prompt blocks fail closed instead of parking the hook worker (#24505)."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from gobby.hooks import shell_execution
from gobby.hooks.shell_execution import ShellExecution, path_execution, read_script_body

pytestmark = pytest.mark.unit

# Generous enough that the read thread is running before the caller gives up on it.
_TIMEOUT = 0.5


@pytest.fixture
def blocked_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[queue.Queue[str]]:
    """Make every file read block, as open() does on an unanswered TCC prompt."""
    gate = threading.Event()
    started: queue.Queue[str] = queue.Queue()
    real_read_prefix = shell_execution._read_prefix

    def blocked_read_prefix(word: str, base: Path | None, limit: int) -> tuple[Path, bytes] | None:
        started.put(word)
        gate.wait(timeout=30)
        return real_read_prefix(word, base, limit)

    executor = ThreadPoolExecutor(max_workers=shell_execution._SCRIPT_READ_WORKERS)
    monkeypatch.setattr(shell_execution, "SCRIPT_READ_TIMEOUT_SECONDS", _TIMEOUT)
    monkeypatch.setattr(shell_execution, "_SCRIPT_READ_EXECUTOR", executor)
    monkeypatch.setattr(shell_execution, "_parked_reads", set())
    monkeypatch.setattr(shell_execution, "_read_prefix", blocked_read_prefix)
    yield started
    gate.set()
    executor.shutdown(wait=True)


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "reset.command"
    script.write_text("#!/usr/bin/env bash\ngit status\n", encoding="utf-8")
    return script


def test_blocked_script_body_read_fails_closed(
    tmp_path: Path, blocked_reads: queue.Queue[str]
) -> None:
    script = _script(tmp_path)

    assert read_script_body(str(script), None) is None
    assert path_execution([str(script)], None) == ShellExecution(script_file=True)
    assert blocked_reads.get(timeout=5) == str(script)
    assert blocked_reads.get(timeout=5) == str(script)


def test_reads_fail_at_once_while_every_read_thread_is_parked(
    tmp_path: Path, blocked_reads: queue.Queue[str]
) -> None:
    script = _script(tmp_path)
    parked: list[Future[tuple[Path, bytes] | None]] = [
        Future() for _ in range(shell_execution._SCRIPT_READ_WORKERS)
    ]
    shell_execution._parked_reads.update(parked)

    assert read_script_body(str(script), None) is None
    assert path_execution([str(script)], None) == ShellExecution(script_file=True)
    assert blocked_reads.empty()


def test_unblocked_script_reads_return_body_and_shell_word(tmp_path: Path) -> None:
    script = _script(tmp_path)

    assert read_script_body(str(script), None) == (
        script.resolve(),
        "#!/usr/bin/env bash\ngit status\n",
    )
    execution = path_execution([str(script)], None)
    assert execution is not None
    assert execution.script_word == str(script.resolve())
