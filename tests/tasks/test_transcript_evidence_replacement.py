"""Stream parity across in-place run replacement, and bounded work per stop (#25143).

A stop resumes from the stream's checkpoint. Runs that a later record rewrites in
place (a background completion or conflicting receipt, an rtk recall, a settled
command output) must reach the stream as replacements, not as duplicates or
dropped updates. The full derivation uses its own session id so its snapshot
never moves the stream's checkpoint behind its back.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.transcript_io import _iter_archive_lines
from gobby.storage.session_models import Session
from gobby.tasks import transcript_evidence
from gobby.tasks.transcript_evidence_cache import clear_snapshots, read_snapshot, write_snapshot
from gobby.tasks.transcript_evidence_delta import lean_runs
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.tasks.transcript_evidence_stream import TranscriptEvidenceStream
from gobby.tasks.transcript_outcomes import EvidenceOutcome
from gobby.tasks.transcript_validation_ledger import TranscriptValidationLedger
from gobby.workflows import validation_cover
from gobby.workflows.found_work_gate import unresolved_validation_failures
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_tool_pair,
    _session,
    _write_jsonl,
)
from tests.tasks.test_transcript_evidence_incremental import _append_jsonl, _derive

_RECALL = "d1965a04eb04"
_RECOVERED = "tests/tasks/test_b.py:7: AssertionError: assert False\n1 failed in 0.01s"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


@pytest.fixture(autouse=True)
def _inline_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the worker entry in-process at the pool boundary."""

    async def run_worker(function: Callable[..., Any], /, *args: object) -> Any:
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_worker)


def _output_path(job_id: str) -> str:
    return f"/private/tmp/claude-test/tasks/{job_id}.output"


def _background_start(
    command: str, call_id: str, job_id: str, at: datetime
) -> list[dict[str, Any]]:
    return _claude_tool_pair(
        command=command,
        call_id=call_id,
        start=at,
        result=(
            "Command did not complete within its 120s timeout and was moved to the "
            f"background (ID: {job_id}). Output is being written to: {_output_path(job_id)}. "
            "You will be notified when it completes."
        ),
    )


def _notice(
    job_id: str, call_id: str, command: str, exit_code: int, at: datetime
) -> dict[str, Any]:
    status = "completed" if exit_code == 0 else "failed"
    return {
        "type": "user",
        "timestamp": at.isoformat(),
        "message": {
            "role": "user",
            "content": (
                f"<task-notification><task-id>{job_id}</task-id>"
                f"<tool-use-id>{call_id}</tool-use-id>"
                f"<output-file>{_output_path(job_id)}</output-file><status>{status}</status>"
                f'<summary>Background command "{command}" {status} with exit code {exit_code}'
                "</summary></task-notification>"
            ),
        },
    }


def _pytest(command: str, call_id: str, at: datetime, exit_code: int) -> list[dict[str, Any]]:
    stdout = "1 failed" if exit_code else "1 passed"
    return _claude_tool_pair(
        command=command,
        call_id=call_id,
        start=at,
        result={"exit_code": exit_code, "stdout": stdout},
    )


def _findings(runs: tuple[TranscriptValidationRun, ...]) -> tuple[TranscriptValidationRun, ...]:
    """Gate-facing failures, with the renumbered position and the derivation's session ignored."""
    unresolved = unresolved_validation_failures(runs, owner_handoff=False)
    return tuple(replace(run, order=0, session_id="") for run in unresolved)


async def _stream_and_full(
    transcript: Path, tmp_path: Path, stream: TranscriptEvidenceStream
) -> tuple[tuple[TranscriptValidationRun, ...], tuple[TranscriptValidationRun, ...]]:
    streamed = await _derive(_session("claude", transcript), BASE_TIME, tmp_path, stream=stream)
    full = await _derive(_session("claude", transcript, suffix="2"), BASE_TIME, tmp_path)
    return streamed.validation_runs, full.validation_runs


@pytest.mark.asyncio
async def test_stream_matches_full_derivation_across_background_completion_and_conflict(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "background.jsonl"
    command = "uv run pytest tests/tasks/test_bg.py -q --tb=line"
    _write_jsonl(transcript, _background_start(command, "bg-call", "job-1", BASE_TIME))
    stream = TranscriptEvidenceStream()
    await _stream_and_full(transcript, tmp_path, stream)

    _append_jsonl(
        transcript,
        [_notice("job-1", "bg-call", command, 1, BASE_TIME + timedelta(seconds=4))],
    )
    streamed, full = await _stream_and_full(transcript, tmp_path, stream)
    assert _findings(full), "the completed background failure is an unresolved finding"
    assert _findings(streamed) == _findings(full)

    # A conflicting second receipt turns the settled failure back into an unknown outcome.
    _append_jsonl(
        transcript,
        [_notice("job-1", "bg-call", command, 0, BASE_TIME + timedelta(seconds=6))],
    )
    streamed, full = await _stream_and_full(transcript, tmp_path, stream)
    assert _findings(full) == ()
    assert _findings(streamed) == _findings(full)


@pytest.mark.asyncio
async def test_stream_matches_full_derivation_across_rtk_recall_replacement(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "recall.jsonl"
    command = "uv run pytest tests/tasks/test_b.py -q"
    truncated = f"Pytest: 0 passed, 1 failed\n[full output: rtk recall {_RECALL}]"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=command,
            call_id="truncated",
            start=BASE_TIME,
            result={"exit_code": 1, "stdout": truncated},
        ),
    )
    stream = TranscriptEvidenceStream()
    await _stream_and_full(transcript, tmp_path, stream)

    _append_jsonl(
        transcript,
        _claude_tool_pair(
            command=f"rtk recall {_RECALL}",
            call_id="recall",
            start=BASE_TIME + timedelta(seconds=10),
            result=_RECOVERED,
        ),
    )
    streamed, full = await _stream_and_full(transcript, tmp_path, stream)
    assert [run.output for run in _findings(full)] == [_RECOVERED]
    assert _findings(streamed) == _findings(full)


@pytest.mark.asyncio
async def test_stop_work_after_resolved_history_does_not_grow_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[int] = []
    written: list[int] = []
    cover_calls: list[int] = []
    real_read = read_snapshot
    real_write = write_snapshot
    real_segments = validation_cover._run_segments

    def counting_read(session_id: str) -> bytes | None:
        payload = real_read(session_id)
        loaded.append(len(payload or b""))
        return payload

    def counting_write(session_id: str, payload: bytes) -> None:
        written.append(len(payload))
        real_write(session_id, payload)

    def counting_segments(run: TranscriptValidationRun) -> Any:
        cover_calls.append(1)
        return real_segments(run)

    monkeypatch.setattr("gobby.tasks.transcript_evidence_snapshots.read_snapshot", counting_read)
    monkeypatch.setattr("gobby.tasks.transcript_evidence_snapshots.write_snapshot", counting_write)
    monkeypatch.setattr(validation_cover, "_run_segments", counting_segments)
    clear_snapshots()

    async def second_stop(pairs: int, suffix: str) -> tuple[int, int, int]:
        """Bytes read, bytes written and cover checks for a stop after one appended run."""
        root = tmp_path / f"history-{pairs}"
        root.mkdir()
        transcript = root / "transcript.jsonl"
        command = "uv run pytest tests/tasks/test_pair.py -q"
        records: list[dict[str, Any]] = []
        for index in range(pairs):
            at = BASE_TIME + timedelta(seconds=10 * index)
            records += _pytest(command, f"red-{index}", at, 1)
            records += _pytest(command, f"green-{index}", at + timedelta(seconds=2), 0)
        _write_jsonl(transcript, records)
        session = _session("claude", transcript, suffix=suffix)
        stream = TranscriptEvidenceStream()
        await _derive(session, BASE_TIME, root, stream=stream)

        _append_jsonl(
            transcript,
            _pytest(
                "uv run ruff check src/", "lint", BASE_TIME + timedelta(seconds=10 * pairs + 5), 0
            ),
        )
        loaded.clear()
        written.clear()
        cover_calls.clear()
        streamed = await _derive(session, BASE_TIME, root, stream=stream)
        # The gate's own pass over the reduced runs is part of the stop's cost.
        assert not unresolved_validation_failures(streamed.validation_runs, owner_handoff=False)
        return sum(loaded), sum(written), len(cover_calls)

    small = await second_stop(6, "3")
    large = await second_stop(120, "4")
    assert large[0] <= small[0] + 1024, "a stop reads no more snapshot than its own history size"
    assert large[1] <= small[1] + 1024, "a stop writes no more snapshot than its own history size"
    assert large[2] <= small[2], "a stop evaluates no more cover checks than its own history size"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
async def test_stream_matches_full_derivation_across_random_replacement_sequences(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    transcript = tmp_path / f"random-{seed}.jsonl"
    stream = TranscriptEvidenceStream()
    pending: list[tuple[str, str, str]] = []
    settled: list[tuple[str, str, str, int]] = []
    truncated: list[str] = []
    recalled: set[str] = set()
    commands = ("uv run pytest tests/tasks/test_a.py -q", "uv run pytest tests/tasks/test_b.py -q")
    for step in range(30):
        at = BASE_TIME + timedelta(seconds=20 * step)
        choice = rng.choice(
            ["red", "green", "ruff", "bg", "settle", "conflict", "truncate", "recall"]
        )
        records: list[dict[str, Any]] = []
        if choice == "red":
            records = _pytest(rng.choice(commands), f"red-{step}", at, 1)
        elif choice == "green":
            records = _pytest(rng.choice(commands), f"green-{step}", at, 0)
        elif choice == "ruff":
            records = _pytest("uv run ruff check src/", f"ruff-{step}", at, rng.choice([0, 1]))
        elif choice == "bg":
            command = rng.choice(commands)
            job = f"job-{seed}-{step}"
            records = _background_start(command, f"bg-{step}", job, at)
            pending.append((job, f"bg-{step}", command))
        elif choice == "settle" and pending:
            job, call, command = pending.pop(rng.randrange(len(pending)))
            exit_code = rng.choice([0, 1])
            records = [_notice(job, call, command, exit_code, at)]
            settled.append((job, call, command, exit_code))
        elif choice == "conflict" and settled:
            job, call, command, exit_code = settled[rng.randrange(len(settled))]
            records = [_notice(job, call, command, 1 - exit_code, at)]
        elif choice == "truncate":
            recall_id = f"recall{seed}{step:04d}"
            truncated.append(recall_id)
            records = _claude_tool_pair(
                command=rng.choice(commands),
                call_id=f"trunc-{step}",
                start=at,
                result={
                    "exit_code": 1,
                    "stdout": f"Pytest: 0 passed, 1 failed\n[full output: rtk recall {recall_id}]",
                },
            )
        elif choice == "recall" and truncated:
            recall_id = truncated[rng.randrange(len(truncated))]
            if recall_id not in recalled:
                recalled.add(recall_id)
                records = _claude_tool_pair(
                    command=f"rtk recall {recall_id}",
                    call_id=f"recall-{step}",
                    start=at,
                    result=f"tests/tasks/test_r.py:{step}: AssertionError\n1 failed in 0.01s",
                )
        if not records:
            continue
        if transcript.exists():
            _append_jsonl(transcript, records)
        else:
            _write_jsonl(transcript, records)
        streamed, full = await _stream_and_full(transcript, tmp_path, stream)
        assert _findings(streamed) == _findings(full), f"seed {seed} step {step} ({choice})"


def _validation_run(command: str, order: int, outcome: EvidenceOutcome) -> TranscriptValidationRun:
    at = BASE_TIME + timedelta(seconds=order)
    return TranscriptValidationRun(
        session_id="session",
        source="claude",
        command=command,
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome=outcome,
        started_at=at,
        completed_at=at,
        order=order,
        exit_code=0 if outcome == "success" else 1,
        run_id=order,
    )


def _settled_background(index: int) -> TranscriptValidationRun:
    run = _validation_run(f"uv run pytest tests/tasks/test_{index}.py -q", index + 1, "success")
    return replace(run, background_job_id=f"job-{index}")


def test_lean_runs_keeps_a_fixed_window_of_settled_background_runs() -> None:
    short = lean_runs(tuple(_settled_background(index) for index in range(100)))
    long = lean_runs(tuple(_settled_background(index) for index in range(1000)))
    assert len(long) == len(short)


def test_ledger_view_keeps_a_fixed_window_of_greens_and_every_open_failure() -> None:
    def view(greens: int) -> tuple[TranscriptValidationRun, ...]:
        failure = _validation_run("uv run pytest tests/tasks/test_fail.py -q", 0, "failure")
        runs = [
            failure,
            *(
                _validation_run(
                    f"uv run pytest tests/tasks/test_ok_{index}.py -q", index + 1, "success"
                )
                for index in range(greens)
            ),
        ]
        return TranscriptValidationLedger.of(None, runs).reduced()

    short = view(100)
    long = view(1000)
    assert len(long) == len(short)
    assert any(run.command.endswith("test_fail.py -q") for run in long)


def _write_archive(session: Session, archive_dir: Path, records: list[dict[str, Any]]) -> None:
    archive = archive_dir / f"{session.external_id}.jsonl.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")


@pytest.mark.asyncio
async def test_archive_is_parsed_once_while_its_identity_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses: list[str] = []
    real_lines = _iter_archive_lines

    def counting_lines(path: str) -> Any:
        parses.append(path)
        return real_lines(path)

    monkeypatch.setattr(transcript_evidence, "_iter_archive_lines", counting_lines)
    clear_snapshots()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    transcript = tmp_path / "claude.jsonl"
    session = _session("claude", transcript)
    _write_archive(
        session,
        archive_dir,
        _pytest(
            "uv run pytest tests/tasks/test_old.py -q", "old", BASE_TIME + timedelta(seconds=1), 0
        ),
    )
    _write_jsonl(
        transcript,
        _pytest(
            "uv run pytest tests/tasks/test_live.py -q",
            "live-0",
            BASE_TIME + timedelta(minutes=1),
            0,
        ),
    )
    stream = TranscriptEvidenceStream()
    counts: list[int] = []
    for step in range(3):
        if step:
            _append_jsonl(
                transcript,
                _pytest(
                    "uv run pytest tests/tasks/test_live.py -q",
                    f"live-{step}",
                    BASE_TIME + timedelta(minutes=1, seconds=10 * step),
                    0,
                ),
            )
        parses.clear()
        await _derive(session, BASE_TIME, tmp_path, stream=stream, archive_dir=str(archive_dir))
        counts.append(len(parses))
    assert counts == [1, 0, 0]


async def _two_transcript_runs(root: Path, greens: int) -> tuple[TranscriptValidationRun, ...]:
    archive_dir = root / "archives"
    archive_dir.mkdir(parents=True)
    transcript = root / "claude.jsonl"
    session = _session("claude", transcript)
    _write_archive(
        session,
        archive_dir,
        _pytest(
            "uv run pytest tests/tasks/test_old.py -q", "old", BASE_TIME + timedelta(seconds=1), 0
        ),
    )
    records: list[dict[str, Any]] = []
    for index in range(greens):
        records += _pytest(
            f"uv run pytest tests/tasks/test_ok_{index}.py -q",
            f"green-{index}",
            BASE_TIME + timedelta(minutes=1, seconds=10 * index),
            0,
        )
    _write_jsonl(transcript, records)
    evidence = await _derive(
        session, BASE_TIME, root, stream=TranscriptEvidenceStream(), archive_dir=str(archive_dir)
    )
    return evidence.validation_runs


@pytest.mark.asyncio
async def test_two_transcript_stream_output_does_not_grow_with_distinct_greens(
    tmp_path: Path,
) -> None:
    clear_snapshots()
    small = await _two_transcript_runs(tmp_path / "small", 100)
    clear_snapshots()
    large = await _two_transcript_runs(tmp_path / "large", 200)
    assert len(large) == len(small)
