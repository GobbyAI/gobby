"""Found-work stop evaluation reads only the latest turn of a long-lived transcript (#25143).

Each stop evaluates the transcript bytes after the boundary stored in the session
variables map. The boundary is a small record of path, offset and a tail hash. A
missing or stale boundary takes a bounded backward read to the latest user prompt.
The byte-count assertions wrap the transcript file object, because the gate swallows
evaluation errors and a read that raises would look like "no failures".
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.session_models import Session
from gobby.tasks import transcript_evidence
from gobby.workflows.found_work_gate import FoundWorkStopAnalyzer
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_tool_pair,
    _session,
    _write_jsonl,
)

pytestmark = pytest.mark.unit

WARM_TURNS = 2000
BULK_TURNS = 2600
FIRST_RED = "uv run pytest tests/tasks/test_a.py -q"
LATER_GREEN = "uv run pytest tests/tasks/test_b.py -q"
LATEST_RED = "uv run pytest tests/tasks/test_c.py -q"
WARM_COMMAND = "uv run pytest tests/tasks/test_warm.py -q"
ONE_MIB = 1024 * 1024
# Slack for the line terminators and record framing around an appended turn.
APPEND_SLACK_BYTES = 4096
# One backward-read chunk. The cap tests allow one extra chunk past the 1 MiB cap.
BACKWARD_CHUNK_BYTES = 64 * 1024


class _ReadSpy:
    """Counts the bytes a watched transcript file hands back, and injects faults."""

    def __init__(self) -> None:
        self.watched: set[Path] = set()
        self.bytes_read = 0
        self.seeks: list[int] = []
        self.fail_next_read = False
        # Runs once after the first watched read returns, so a writer can append
        # while the stop is in flight.
        self.after_first_read: Callable[[], Any] | None = None

    def reset(self) -> None:
        self.bytes_read = 0
        self.seeks = []

    def watches(self, file: object) -> bool:
        if not isinstance(file, str | os.PathLike):
            return False
        return Path(file).resolve() in self.watched


class _CountingFile:
    def __init__(self, handle: Any, spy: _ReadSpy) -> None:
        self._handle = handle
        self._spy = spy

    def _before_read(self) -> None:
        if self._spy.fail_next_read:
            self._spy.fail_next_read = False
            raise OSError("injected transcript read failure")

    def _after_read(self, count: int) -> None:
        self._spy.bytes_read += count
        hook = self._spy.after_first_read
        if hook is not None:
            self._spy.after_first_read = None
            hook()

    def read(self, size: int = -1) -> Any:
        self._before_read()
        data = self._handle.read(size)
        self._after_read(len(data))
        return data

    def readline(self, size: int = -1) -> Any:
        self._before_read()
        line = self._handle.readline(size)
        self._after_read(len(line))
        return line

    def readlines(self) -> list[Any]:
        self._before_read()
        lines: list[Any] = self._handle.readlines()
        self._after_read(sum(len(line) for line in lines))
        return lines

    def readinto(self, buffer: Any) -> int:
        self._before_read()
        count: int = self._handle.readinto(buffer)
        self._after_read(count)
        return count

    def __iter__(self) -> Iterator[Any]:
        self._before_read()
        for line in self._handle:
            self._spy.bytes_read += len(line)
            yield line
        self._after_read(0)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._spy.seeks.append(offset)
        position: int = self._handle.seek(offset, whence)
        return position

    def __enter__(self) -> _CountingFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._handle.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


@pytest.fixture
def in_process_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the evidence worker in this process so the read spy sees its reads."""

    async def run_worker(function: Callable[..., Any], /, *args: object) -> Any:
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_worker)


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> _ReadSpy:
    spy = _ReadSpy()

    def wrap(real_open: Callable[..., Any]) -> Callable[..., Any]:
        def opener(file: Any, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, *args, **kwargs)
            return _CountingFile(handle, spy) if spy.watches(file) else handle

        return opener

    # Path.open resolves io.open at call time, so both names need the wrapper.
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    return spy


def _at(seconds: int) -> datetime:
    return BASE_TIME + timedelta(seconds=seconds)


def _prompt(text: str, at: int) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _at(at).isoformat(),
        "message": {"role": "user", "content": text},
    }


def _turn(
    *, prompt: str, command: str, call_id: str, result: dict[str, Any], at: int
) -> list[dict[str, Any]]:
    return [
        _prompt(prompt, at),
        *_claude_tool_pair(command=command, call_id=call_id, start=_at(at + 1), result=result),
    ]


def _warm_records(turns: int, *, prefix: str, first_at: int = 0) -> list[dict[str, Any]]:
    """Green validation turns that leave nothing unresolved."""
    return [
        record
        for index in range(turns)
        for record in _turn(
            prompt=f"{prefix} prompt {index}",
            command=WARM_COMMAND,
            call_id=f"{prefix}-{index}",
            result={"exit_code": 0, "stdout": "4 passed"},
            at=first_at + 3 * index,
        )
    ]


def _append(transcript: Path, records: list[dict[str, Any]]) -> int:
    """Append records as JSONL and return the bytes written."""
    data = "".join(json.dumps(record) + "\n" for record in records)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(data)
    return len(data.encode("utf-8"))


def _append_latest_red(transcript: Path, *, at: int) -> int:
    return _append(
        transcript,
        _turn(
            prompt="latest prompt",
            command=LATEST_RED,
            call_id="latest-red",
            result={"exit_code": 1, "stdout": "1 failed"},
            at=at,
        ),
    )


def _analyzer(session: Session) -> FoundWorkStopAnalyzer:
    return FoundWorkStopAnalyzer(
        llm_service_resolver=lambda: None,
        config_resolver=lambda: None,
        session_manager=SimpleNamespace(get=lambda _session_id: session),
        session_task_manager=SimpleNamespace(get_session_tasks=lambda _session_id: []),
    )


async def _stop_failures(
    analyzer: FoundWorkStopAnalyzer,
    session: Session,
    project_path: Path,
    variables: dict[str, Any],
) -> tuple[str, ...]:
    return await analyzer._terminal_failures(
        session_id=session.id,
        variables=variables,
        project_path=str(project_path),
        project_id=None,
    )


async def test_earlier_turn_failure_is_not_reraised_at_a_later_stop(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        _turn(
            prompt="first prompt",
            command=FIRST_RED,
            call_id="first-red",
            result={"exit_code": 1, "stdout": "1 failed"},
            at=0,
        ),
    )
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (FIRST_RED,)

    _append(
        transcript,
        _turn(
            prompt="second prompt",
            command=LATER_GREEN,
            call_id="later-green",
            result={"exit_code": 0, "stdout": "4 passed"},
            at=10,
        ),
    )

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()


async def test_repeated_stop_in_the_same_turn_still_reports_its_failure(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    _append_latest_red(transcript, at=3 * WARM_TURNS + 10)
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)

    # The blocked stop continues the turn with no new prompt and no covering run.
    _append(
        transcript,
        _claude_tool_pair(
            command="git status --short",
            call_id="same-turn",
            start=_at(3 * WARM_TURNS + 20),
            result={"exit_code": 0, "stdout": ""},
        ),
    )

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)


async def test_latest_turn_terminal_failure_is_reported_after_a_large_prior_transcript(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    _append_latest_red(transcript, at=3 * WARM_TURNS + 10)

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)


async def test_stop_resumes_from_the_boundary_stored_in_session_variables(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    appended = _append_latest_red(transcript, at=3 * WARM_TURNS + 10)
    read_spy.reset()

    # A fresh analyzer has no in-memory state; only the variables map carries over.
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read <= appended + APPEND_SLACK_BYTES, (
        f"second stop read {read_spy.bytes_read} bytes for a {appended}-byte appended turn"
    )


async def test_first_stop_without_a_stored_boundary_takes_a_bounded_backward_read(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        _turn(
            prompt="first prompt",
            command=FIRST_RED,
            call_id="first-red",
            result={"exit_code": 1, "stdout": "1 failed"},
            at=0,
        )
        + _warm_records(WARM_TURNS, prefix="warm", first_at=10),
    )
    _append_latest_red(transcript, at=3 * WARM_TURNS + 20)
    size = transcript.stat().st_size
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)

    # The variables map has no boundary key, so this stop must not scan the whole file.
    assert await _stop_failures(_analyzer(session), session, tmp_path, {}) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read < size // 2, (
        f"first stop read {read_spy.bytes_read} bytes of a {size}-byte transcript"
    )


async def test_stop_persists_the_bytes_it_read_not_the_size_at_start(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    _append(
        transcript,
        _turn(
            prompt="middle prompt",
            command=LATER_GREEN,
            call_id="middle-green",
            result={"exit_code": 0, "stdout": "4 passed"},
            at=3 * WARM_TURNS + 5,
        ),
    )
    read_bytes = transcript.stat().st_size
    # A writer appends a turn after this stop's first read. The stop has read only
    # up to read_bytes, so the boundary it stores must stop there.
    read_spy.after_first_read = lambda: _append_latest_red(transcript, at=3 * WARM_TURNS + 10)
    read_spy.reset()
    during = await _stop_failures(_analyzer(session), session, tmp_path, variables)

    boundaries = [value for value in variables.values() if isinstance(value, dict)]
    assert [boundary.get("offset") for boundary in boundaries if "offset" in boundary] == [
        read_bytes
    ]

    after = await _stop_failures(_analyzer(session), session, tmp_path, variables)

    assert LATEST_RED in during + after


async def test_failed_stop_keeps_the_previous_boundary(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    appended = _append_latest_red(transcript, at=3 * WARM_TURNS + 10)
    read_spy.fail_next_read = True
    await _stop_failures(_analyzer(session), session, tmp_path, variables)
    assert not read_spy.fail_next_read, "the injected read failure never fired"

    read_spy.reset()
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read <= appended + APPEND_SLACK_BYTES, (
        f"stop after a failed stop read {read_spy.bytes_read} bytes "
        f"for a {appended}-byte appended turn"
    )


async def test_resume_after_a_large_append_reads_at_most_one_mebibyte(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    bulk_first_at = 3 * WARM_TURNS
    _append(transcript, _warm_records(BULK_TURNS, prefix="bulk", first_at=bulk_first_at))
    _append_latest_red(transcript, at=3 * (WARM_TURNS + BULK_TURNS) + 10)
    read_spy.reset()

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read <= ONE_MIB + BACKWARD_CHUNK_BYTES, (
        f"resume read {read_spy.bytes_read} bytes after a large append"
    )


async def test_truncated_transcript_with_a_stale_stored_boundary_takes_a_bounded_backward_read(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    # Truncation: the stored boundary now points past a shorter file.
    truncated_turns = WARM_TURNS // 2
    _write_jsonl(transcript, _warm_records(truncated_turns, prefix="truncated"))
    _append_latest_red(transcript, at=3 * truncated_turns + 10)
    size = transcript.stat().st_size
    read_spy.reset()

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read < size // 2, (
        f"stop after truncation read {read_spy.bytes_read} bytes of a {size}-byte transcript"
    )


async def test_replaced_transcript_with_a_longer_body_fails_the_tail_hash(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    # Replacement: the file is three times longer, so the stored offset still lands
    # inside it. Only the tail hash at that offset tells the stale boundary apart.
    replaced_turns = 3 * WARM_TURNS
    _write_jsonl(transcript, _warm_records(replaced_turns, prefix="replaced"))
    _append_latest_red(transcript, at=3 * replaced_turns + 10)
    size = transcript.stat().st_size
    read_spy.reset()

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    assert 0 < read_spy.bytes_read < size // 2, (
        f"stop after replacement read {read_spy.bytes_read} bytes of a {size}-byte transcript"
    )


async def test_stop_past_a_multi_mebibyte_transcript_reads_only_the_latest_turn(
    tmp_path: Path, in_process_pool: None, read_spy: _ReadSpy
) -> None:
    """Megabytes of earlier turns cost the stop only the latest turn and one backward chunk."""
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _warm_records(WARM_TURNS, prefix="warm"))
    read_spy.watched.add(transcript.resolve())
    session = _session("claude", transcript)
    variables: dict[str, Any] = {}
    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == ()

    _append(transcript, _warm_records(BULK_TURNS, prefix="bulk", first_at=3 * WARM_TURNS))
    latest_bytes = _append_latest_red(transcript, at=3 * (WARM_TURNS + BULK_TURNS) + 10)
    size = transcript.stat().st_size
    assert size > 2 * ONE_MIB, size
    assert latest_bytes < BACKWARD_CHUNK_BYTES, latest_bytes
    read_spy.reset()

    assert await _stop_failures(_analyzer(session), session, tmp_path, variables) == (LATEST_RED,)
    # One backward chunk, whatever the transcript's size: the cost never scales with the file.
    assert read_spy.bytes_read <= BACKWARD_CHUNK_BYTES + APPEND_SLACK_BYTES, (
        f"stop read {read_spy.bytes_read} bytes of a {size}-byte transcript; "
        f"the latest turn is {latest_bytes} bytes"
    )
