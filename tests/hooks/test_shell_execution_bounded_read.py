"""Script reads a privacy prompt blocks fail closed instead of parking the hook worker (#24505)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.hooks import shell_execution
from gobby.hooks.shell_execution import ShellExecution, path_execution, read_script_body

pytestmark = pytest.mark.unit

_TIMEOUT = 0.05


@pytest.fixture
def blocked_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Make every file read block, as open() does on an unanswered TCC prompt."""
    gate = threading.Event()
    started: list[str] = []
    real_read_prefix = shell_execution._read_prefix

    def blocked_read_prefix(word: str, base: Path | None, limit: int) -> tuple[Path, bytes] | None:
        started.append(word)
        gate.wait(timeout=30)
        return real_read_prefix(word, base, limit)

    monkeypatch.setattr(shell_execution, "SCRIPT_READ_TIMEOUT_SECONDS", _TIMEOUT)
    monkeypatch.setattr(shell_execution, "_read_prefix", blocked_read_prefix)
    yield started
    gate.set()
    for _ in range(200):
        if not shell_execution._parked_reads:
            return
        time.sleep(0.01)
    raise AssertionError("parked script reads never finished")


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "reset.command"
    script.write_text("#!/usr/bin/env bash\ngit status\n", encoding="utf-8")
    return script


def test_blocked_script_body_read_fails_closed(tmp_path: Path, blocked_reads: list[str]) -> None:
    script = _script(tmp_path)

    assert read_script_body(str(script), None) is None
    assert path_execution([str(script)], None) == ShellExecution(script_file=True)
    assert blocked_reads == [str(script), str(script)]


def test_reads_fail_at_once_while_every_read_thread_is_parked(
    tmp_path: Path, blocked_reads: list[str]
) -> None:
    script = _script(tmp_path)
    for _ in range(shell_execution._SCRIPT_READ_WORKERS):
        assert read_script_body(str(script), None) is None

    started = time.monotonic()
    assert read_script_body(str(script), None) is None

    assert time.monotonic() - started < _TIMEOUT
    assert len(blocked_reads) == shell_execution._SCRIPT_READ_WORKERS


def test_unblocked_script_reads_return_body_and_shell_word(tmp_path: Path) -> None:
    script = _script(tmp_path)

    assert read_script_body(str(script), None) == (
        script.resolve(),
        "#!/usr/bin/env bash\ngit status\n",
    )
    execution = path_execution([str(script)], None)
    assert execution is not None
    assert execution.script_word == str(script.resolve())
