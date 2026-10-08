"""Incremental close-evidence derivation: watermarks, resume, and fallbacks."""

from __future__ import annotations

import asyncio
import gzip
import json
import multiprocessing
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.sessions.transcripts.base import RawLine
from gobby.storage.session_models import Session
from gobby.tasks import (
    transcript_evidence,
    transcript_evidence_cache,
    transcript_evidence_snapshots,
    transcript_exclusions,
)
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_evidence_snapshots import clear_evidence_snapshots
from gobby.tasks.transcript_evidence_transfer import ChunkedPayload, decode
from gobby.tasks.transcript_tool_arguments import normalize_known_path
from gobby.workflows.found_work_gate import (
    _reported_failure_paths,
    unresolved_validation_failures,
)
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_tool_pair,
    _codex_response_item,
    _session,
    _write_jsonl,
)

DETECTION = default_validation_detection_config()


def _snapshot_key(
    session: Session,
    repo_path: Path,
    window: datetime | None = BASE_TIME,
    task_files: set[str] | None = None,
) -> str:
    fingerprint = transcript_evidence._derivation_fingerprint(
        session,
        window,
        DETECTION,
        {normalize_known_path(item, str(repo_path)) for item in task_files or set()},
        str(repo_path),
        None,
    )
    return f"{session.id}:{fingerprint}"


def _stored_snapshot(
    session: Session,
    repo_path: Path,
    window: datetime | None = BASE_TIME,
    task_files: set[str] | None = None,
) -> transcript_evidence_snapshots.EvidenceSnapshot | None:
    return transcript_evidence_snapshots.load_durable_snapshot(
        _snapshot_key(session, repo_path, window, task_files)
    )


Caller = Literal["window", "prelink"]
Derived = TranscriptEvidence | tuple[TranscriptValidationRun, ...]
PoolCall = tuple[int, ChunkedPayload]


@pytest.fixture
def pool_calls(monkeypatch: pytest.MonkeyPatch) -> list[PoolCall]:
    """Run actual worker entries off-loop and observe only the pool boundary."""
    calls: list[PoolCall] = []

    async def run_worker(
        function: Callable[..., ChunkedPayload], /, *args: object
    ) -> ChunkedPayload:
        payload = await asyncio.to_thread(function, *args)
        calls.append((len(args), payload))
        return payload

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_worker)
    monkeypatch.setattr(transcript_exclusions, "run_in_transcript_evidence_pool", run_worker)
    return calls


async def _derive_for_caller(
    caller: Caller, session: Session, window: datetime, repo: Path
) -> Derived:
    if caller == "prelink":
        return await transcript_exclusions.derive_prelink_runs(
            session, window, DETECTION, str(repo)
        )
    return await derive_transcript_evidence(session, window, DETECTION, set(), str(repo))


def _assert_window_transfer(caller: Caller, result: Derived, call: PoolCall) -> None:
    argument_count, payload = call
    assert argument_count == (8 if caller == "window" else 6), "no outbound resume argument"
    assert decode(payload) == result, "the worker returns only the requested evidence"
    records = (
        len(result.validation_runs) + len(result.command_runs) + len(result.edits)
        if isinstance(result, TranscriptEvidence)
        else len(result)
    )
    assert payload.record_count == records, "no session-wide snapshot records cross the boundary"


def _validation_records(
    call_id: str, at: datetime, outcome: str = "passed"
) -> list[dict[str, Any]]:
    return _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id=call_id,
        start=at,
        result={"exit_code": 0 if outcome == "passed" else 1, "stdout": f"1 {outcome}"},
        is_error=outcome != "passed",
    )


async def test_alternating_callers_resume_their_own_derivation_fingerprint(
    tmp_path: Path, pool_calls: list[PoolCall]
) -> None:
    transcript = tmp_path / "alternating.jsonl"
    _write_jsonl(
        transcript,
        _validation_records("before", BASE_TIME)
        + _validation_records("after", BASE_TIME + timedelta(seconds=3)),
    )
    session = _session("claude", transcript)
    credited_start = BASE_TIME + timedelta(seconds=2)
    prelink_start = BASE_TIME + timedelta(days=1)
    credited = await _derive_for_caller("window", session, credited_start, tmp_path)
    prelink = await _derive_for_caller("prelink", session, prelink_start, tmp_path)
    assert isinstance(credited, TranscriptEvidence)
    assert len(credited.validation_runs) == 1
    assert isinstance(prelink, tuple) and len(prelink) == 2
    prior_watermark = transcript.stat().st_size

    _append_jsonl(transcript, _validation_records("append", BASE_TIME + timedelta(seconds=4)))
    callers: tuple[tuple[Caller, datetime], ...] = (
        ("window", credited_start),
        ("prelink", prelink_start),
    )
    resumed: dict[Caller, Derived] = {}
    for caller, start in callers:
        result = await _derive_for_caller(caller, session, start, tmp_path)
        resumed[caller] = result
        snapshot = _stored_snapshot(session, tmp_path, start if caller == "window" else None)
        assert snapshot is not None and snapshot.parsed_from_offset == prior_watermark
        _assert_window_transfer(caller, result, pool_calls[-1])
    clear_evidence_snapshots()
    for caller, start in callers:
        assert resumed[caller] == await _derive_for_caller(caller, session, start, tmp_path)


@pytest.mark.parametrize("caller", ["window", "prelink"])
async def test_long_transcript_derive_sends_no_resume_payload_and_returns_window_records_only(
    tmp_path: Path, caller: Caller, pool_calls: list[PoolCall]
) -> None:
    transcript = tmp_path / "long.jsonl"
    window = BASE_TIME + timedelta(seconds=1000)
    filler_at = BASE_TIME if caller == "window" else window + timedelta(seconds=1)
    records = [
        record
        for index in range(600)
        for record in _validation_records(
            f"filler-{index}", filler_at + timedelta(microseconds=index)
        )
    ]
    selected_at = window + timedelta(seconds=1) if caller == "window" else BASE_TIME
    records.extend(_validation_records("selected", selected_at))
    _write_jsonl(transcript, records)
    session = _session("claude", transcript)
    first = await _derive_for_caller(caller, session, window, tmp_path)
    second = await _derive_for_caller(caller, session, window, tmp_path)
    assert second == first
    assert pool_calls[-1][1].record_count == 1
    _assert_window_transfer(caller, second, pool_calls[-1])


@pytest.mark.parametrize("caller", ["window", "prelink"])
async def test_worker_owned_snapshot_matches_cold_derive_across_append_truncation_and_rewrite(
    tmp_path: Path, caller: Caller, pool_calls: list[PoolCall]
) -> None:
    transcript = tmp_path / "mutating.jsonl"
    window = BASE_TIME if caller == "window" else BASE_TIME + timedelta(days=1)
    initial = _validation_records("initial", BASE_TIME)
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)
    await _derive_for_caller(caller, session, window, tmp_path)
    initial_snapshot = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert initial_snapshot is not None

    appended = _validation_records("append", BASE_TIME + timedelta(seconds=1))
    _append_jsonl(transcript, appended)
    appended_result = await _derive_for_caller(caller, session, window, tmp_path)
    resumed = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert resumed is not None
    assert resumed.parsed_from_offset == initial_snapshot.watermark
    _assert_window_transfer(caller, appended_result, pool_calls[-1])
    clear_evidence_snapshots()
    assert appended_result == await _derive_for_caller(caller, session, window, tmp_path)

    _write_jsonl(transcript, initial)
    truncated = await _derive_for_caller(caller, session, window, tmp_path)
    snapshot = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert snapshot is not None and snapshot.parsed_from_offset == 0
    clear_evidence_snapshots()
    assert truncated == await _derive_for_caller(caller, session, window, tmp_path)

    original_bytes = transcript.read_bytes()
    # A Claude success records no exit code, so the same-size rewrite flips the
    # runner summary that classifies it.
    transcript.write_bytes(original_bytes.replace(b"1 passed", b"1 failed"))
    assert transcript.stat().st_size == len(original_bytes)
    rewritten = await _derive_for_caller(caller, session, window, tmp_path)
    snapshot = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert snapshot is not None and snapshot.parsed_from_offset == 0
    clear_evidence_snapshots()
    assert rewritten == await _derive_for_caller(caller, session, window, tmp_path)
    assert rewritten != truncated


@pytest.mark.parametrize("caller", ["window", "prelink"])
async def test_cross_process_resume_parses_from_prior_watermark(
    tmp_path: Path, caller: Caller, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[PoolCall] = []

    async def fresh_worker(
        function: Callable[..., ChunkedPayload], /, *args: object
    ) -> ChunkedPayload:
        # Each call starts a genuinely new process with only the durable cache.
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            payload = await asyncio.get_running_loop().run_in_executor(
                pool, partial(function, *args)
            )
        calls.append((len(args), payload))
        return payload

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", fresh_worker)
    monkeypatch.setattr(transcript_exclusions, "run_in_transcript_evidence_pool", fresh_worker)
    transcript = tmp_path / "process.jsonl"
    _write_jsonl(transcript, _validation_records("first", BASE_TIME))
    session = _session("claude", transcript)
    window = BASE_TIME if caller == "window" else BASE_TIME + timedelta(days=1)
    await _derive_for_caller(caller, session, window, tmp_path)
    stored = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert stored is not None
    _append_jsonl(transcript, _validation_records("second", BASE_TIME + timedelta(seconds=1)))
    resumed = await _derive_for_caller(caller, session, window, tmp_path)
    advanced = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert advanced is not None
    assert advanced.parsed_from_offset == stored.watermark
    assert advanced.watermark == transcript.stat().st_size
    _assert_window_transfer(caller, resumed, calls[-1])


@pytest.mark.parametrize("caller", ["window", "prelink"])
async def test_concurrent_derives_equal_cold_and_leave_valid_snapshot(
    tmp_path: Path, caller: Caller, pool_calls: list[PoolCall]
) -> None:
    transcript = tmp_path / "concurrent.jsonl"
    _write_jsonl(transcript, _validation_records("first", BASE_TIME))
    session = _session("claude", transcript)
    window = BASE_TIME if caller == "window" else BASE_TIME + timedelta(days=1)
    cold = await _derive_for_caller(caller, session, window, tmp_path)
    expected_snapshot = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert expected_snapshot is not None
    clear_evidence_snapshots()
    first, second = await asyncio.gather(
        _derive_for_caller(caller, session, window, tmp_path),
        _derive_for_caller(caller, session, window, tmp_path),
    )
    assert first == second == cold
    snapshot = _stored_snapshot(session, tmp_path, window if caller == "window" else None)
    assert snapshot is not None
    assert snapshot.fingerprint == expected_snapshot.fingerprint
    assert snapshot.watermark == transcript.stat().st_size
    assert (
        transcript_evidence_snapshots.read_transcript_suffix(str(transcript), snapshot) is not None
    )
    _assert_window_transfer(caller, first, pool_calls[-2])
    _assert_window_transfer(caller, second, pool_calls[-1])


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


@pytest.fixture
def parse_counts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many raw lines each derivation hands to the parse pipeline."""
    counts: list[int] = []
    original = transcript_evidence.select_window_raw_lines

    def counting(lines: Any, window_start: datetime | None) -> Iterator[RawLine]:
        material = list(lines)
        counts.append(len(material))
        return original(material, window_start)

    monkeypatch.setattr(transcript_evidence, "select_window_raw_lines", counting)

    async def run_inline(function: Any, /, *args: Any) -> Any:
        return function(*args)

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_inline)
    return counts


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")


def _claude_edit(path: str, *, call_id: str, at: datetime) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": at.isoformat(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Edit",
                    "input": {"file_path": path},
                }
            ],
        },
    }


async def _derive(
    session: Any,
    window_start: datetime | None,
    task_files: set[str],
    repo_path: Path,
    **kwargs: Any,
) -> TranscriptEvidence:
    return await derive_transcript_evidence(
        session,
        window_start,
        DETECTION,
        task_files,
        str(repo_path),
        **kwargs,
    )


async def test_second_derivation_parses_only_appended_lines(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    transcript = tmp_path / "claude.jsonl"
    initial = [
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_a.py",
            call_id="run-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "passed"},
        ),
        _claude_edit(
            str(tmp_path / "src" / "changed.py"),
            call_id="edit-1",
            at=BASE_TIME + timedelta(seconds=2),
        ),
    ]
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)

    first = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)
    stored = _stored_snapshot(session, tmp_path, task_files={"src/changed.py"})
    assert stored is not None
    assert stored.watermark == transcript.stat().st_size

    appended = _claude_tool_pair(
        command="uv run ruff check src/",
        call_id="run-2",
        start=BASE_TIME + timedelta(seconds=30),
        result={"exit_code": 0, "stdout": ""},
    )
    _append_jsonl(transcript, appended)

    second = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)

    # The second derivation parsed only the appended suffix, and its watermark
    # advanced past the untouched prefix to the new end of file.
    assert parse_counts == [len(initial), len(appended)]
    advanced = _stored_snapshot(session, tmp_path, task_files={"src/changed.py"})
    assert advanced is not None
    assert advanced.watermark == transcript.stat().st_size
    assert advanced.watermark > stored.watermark

    assert [run.command for run in second.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py",
        "uv run ruff check src/",
    ]
    assert second.validation_runs[: len(first.validation_runs)] == first.validation_runs
    assert second.edits == first.edits


async def test_restart_resumes_from_durable_checkpoint(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    transcript = tmp_path / "restart.jsonl"
    initial = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 1, "stdout": "failed"},
        is_error=True,
    )
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)
    first = await _derive(session, BASE_TIME, set(), tmp_path)
    assert first.validation_runs[0].outcome == "failure"

    # Every invocation now reloads the durable checkpoint, as a fresh worker does.
    appended = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-2",
        start=BASE_TIME + timedelta(seconds=30),
        result={"exit_code": 0, "stdout": "passed"},
    )
    _append_jsonl(transcript, appended)
    second = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(initial), len(appended)]
    assert [run.outcome for run in second.validation_runs] == ["failure", "success"]


@pytest.mark.parametrize(
    "corruption", ["invalid-json", "negative-watermark", "tail-overrun", "tail-digest"]
)
async def test_invalid_durable_checkpoint_reparses_safely(
    tmp_path: Path, parse_counts: list[int], corruption: str
) -> None:
    transcript = tmp_path / "invalid-checkpoint.jsonl"
    records = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, records)
    session = _session("claude", transcript)
    await _derive(session, BASE_TIME, set(), tmp_path)
    checkpoint = transcript_evidence_cache._snapshot_path(_snapshot_key(session, tmp_path))
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    if corruption == "invalid-json":
        checkpoint.write_bytes(b"invalid JSON")
    else:
        payload = json.loads(checkpoint.read_text())
        if corruption == "negative-watermark":
            payload.update(watermark=-1, tail_len=0)
        elif corruption == "tail-overrun":
            payload.update(watermark=1, tail_len=2)
        else:
            payload["tail_sha256"] = "0" * 64
        checkpoint.write_text(json.dumps(payload))

    evidence = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(records), len(records)]
    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py"
    ]
    await _derive(session, BASE_TIME, set(), tmp_path)
    assert parse_counts == [len(records), len(records), 0]


def test_durable_checkpoint_limits_file_and_total_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcript_evidence_cache, "_MAX_SNAPSHOT_BYTES", 16)
    monkeypatch.setattr(transcript_evidence_cache, "_MAX_CACHE_BYTES", 20)
    transcript_evidence_cache.write_snapshot("first", b"a" * 16)
    transcript_evidence_cache.write_snapshot("second", b"b" * 16)
    assert (
        sum(path.stat().st_size for path in transcript_evidence_cache._cache_dir().glob("*.json"))
        <= 20
    )

    transcript_evidence_cache.write_snapshot("oversize", b"c" * 17)
    assert not transcript_evidence_cache._snapshot_path("oversize").exists()
    transcript_evidence_cache._snapshot_path("external").write_bytes(b"d" * 17)
    assert transcript_evidence_cache.read_snapshot("external") is None


async def test_pooled_derivation_keeps_snapshot_resume(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude-pooled.jsonl"
    initial = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)

    first = await _derive(session, BASE_TIME, set(), tmp_path)
    stored = _stored_snapshot(session, tmp_path)
    assert stored is not None
    assert stored.parsed_from_offset == 0

    appended = _claude_tool_pair(
        command="uv run ruff check src/",
        call_id="run-2",
        start=BASE_TIME + timedelta(seconds=30),
        result={"exit_code": 0, "stdout": "All checks passed!"},
    )
    _append_jsonl(transcript, appended)
    second = await _derive(session, BASE_TIME, set(), tmp_path)

    advanced = _stored_snapshot(session, tmp_path)
    assert advanced is not None
    assert advanced.parsed_from_offset == stored.watermark
    assert advanced.watermark == transcript.stat().st_size
    assert advanced.watermark > stored.watermark
    assert second.validation_runs[: len(first.validation_runs)] == first.validation_runs
    assert [run.command for run in second.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py",
        "uv run ruff check src/",
    ]


async def test_repeat_derivation_of_an_unchanged_file_parses_nothing(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    transcript = tmp_path / "claude.jsonl"
    initial = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)

    first = await _derive(session, BASE_TIME, set(), tmp_path)
    second = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(initial), 0]
    assert second == first


@pytest.mark.asyncio
async def test_supplemental_transcripts_resume_from_the_session_snapshot(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    transcript = tmp_path / "transcript-evidence-claude-1.jsonl"
    subagent = transcript.with_suffix("") / "subagents" / "agent-worker.jsonl"
    subagent.parent.mkdir(parents=True)
    primary_records = _claude_tool_pair(
        command="uv run ruff check src/",
        call_id="primary-run",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": ""},
    )
    subagent_records = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="subagent-run",
        start=BASE_TIME + timedelta(seconds=10),
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, primary_records)
    _write_jsonl(subagent, subagent_records)
    session = _session("claude", transcript)

    first = await _derive(session, BASE_TIME, set(), tmp_path)
    second = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(primary_records), len(subagent_records), 0, 0]
    assert second == first
    snapshot = _stored_snapshot(session, tmp_path)
    assert snapshot is not None
    assert snapshot.transcript_path == str(transcript)
    assert list(snapshot.supplemental) == [str(subagent)]

    # After a restart the durable checkpoint still carries the subagent watermark.
    appended = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_b.py",
        call_id="subagent-run-2",
        start=BASE_TIME + timedelta(seconds=20),
        result={"exit_code": 0, "stdout": "passed"},
    )
    _append_jsonl(subagent, appended)
    parse_counts.clear()
    resumed = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [0, len(appended)]
    clear_evidence_snapshots()
    parse_counts.clear()
    assert await _derive(session, BASE_TIME, set(), tmp_path) == resumed
    assert parse_counts == [len(primary_records), len(subagent_records) + len(appended)]


async def test_incremental_derivation_matches_a_full_window_parse(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """Differential: resumed evidence is identical to a fresh full parse."""
    transcript = tmp_path / "claude.jsonl"
    task_files = {"src/changed.py", "src/other.py"}
    prefix = [
        # Older than the window lookback: dropped by window selection either way.
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_old.py",
            call_id="run-old",
            start=BASE_TIME - timedelta(hours=3),
            result={"exit_code": 0, "stdout": "passed"},
        ),
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_a.py",
            call_id="run-pass",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "12 passed"},
        ),
        *_claude_tool_pair(
            command="uv run ruff check src/",
            call_id="run-fail",
            start=BASE_TIME + timedelta(seconds=5),
            result={"exit_code": 1, "stdout": "Found 3 errors."},
            is_error=True,
        ),
        _claude_edit(
            str(tmp_path / "src" / "changed.py"),
            call_id="edit-1",
            at=BASE_TIME + timedelta(seconds=8),
        ),
        _claude_edit(
            str(tmp_path / "src" / "untracked.py"),
            call_id="edit-2",
            at=BASE_TIME + timedelta(seconds=9),
        ),
        # A validation run whose begin sits in the prefix while its result is
        # appended later: the pending pair straddles the watermark.
        {
            "type": "assistant",
            "timestamp": (BASE_TIME + timedelta(seconds=10)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "run-straddle",
                        "name": "Bash",
                        "input": {"command": "uv run mypy src/"},
                    }
                ],
            },
        },
    ]
    suffix = [
        {
            "type": "user",
            "timestamp": (BASE_TIME + timedelta(seconds=40)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "run-straddle",
                        "content": {"exit_code": 0, "stdout": "Success: no issues"},
                        "is_error": False,
                    }
                ],
            },
        },
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_b.py",
            call_id="run-late",
            start=BASE_TIME + timedelta(seconds=50),
            result={"exit_code": 0, "stdout": "3 passed"},
        ),
        _claude_edit(
            str(tmp_path / "src" / "other.py"),
            call_id="edit-3",
            at=BASE_TIME + timedelta(seconds=55),
        ),
    ]
    _write_jsonl(transcript, prefix)
    session = _session("claude", transcript)

    await _derive(session, BASE_TIME, task_files, tmp_path)
    _append_jsonl(transcript, suffix)
    incremental = await _derive(session, BASE_TIME, task_files, tmp_path)
    assert parse_counts == [len(prefix), len(suffix)]

    clear_evidence_snapshots()
    full = await _derive(session, BASE_TIME, task_files, tmp_path)
    assert parse_counts[-1] == len(prefix) + len(suffix)

    assert incremental == full
    assert len(incremental.validation_runs) == 4
    assert [run.outcome for run in incremental.validation_runs] == [
        "success",
        "failure",
        "success",
        "success",
    ]
    assert [edit.path for edit in incremental.edits] == ["src/changed.py", "src/other.py"]


async def test_codex_execution_chain_survives_restart_and_matches_full_parse(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """Codex cross-line execution state survives a daemon process restart."""
    transcript = tmp_path / "codex.jsonl"
    patch = "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
    prefix = [
        _codex_response_item(
            {
                "type": "custom_tool_call",
                "call_id": "outer-exec",
                "name": "exec",
                "input": (
                    'const r = await tools.exec_command({cmd:"uv run pytest tests/tasks"}); '
                    "text(r);"
                ),
            },
            BASE_TIME,
        ),
    ]
    suffix = [
        _codex_response_item(
            {
                "type": "custom_tool_call_output",
                "call_id": "outer-exec",
                "output": json.dumps({"exit_code": 0, "output": "passed"}),
            },
            BASE_TIME + timedelta(seconds=20),
        ),
        _codex_response_item(
            {
                "type": "custom_tool_call",
                "call_id": "patch-1",
                "name": "apply_patch",
                "input": patch,
            },
            BASE_TIME + timedelta(seconds=25),
        ),
        # An exec chain without structured terminal metadata: the outcome is
        # unknown and the evidence degrades — on both derivation paths.
        _codex_response_item(
            {
                "type": "custom_tool_call",
                "call_id": "outer-unknown",
                "name": "exec",
                "input": 'const r = await tools.exec_command({cmd:"pytest"}); text(r.output);',
            },
            BASE_TIME + timedelta(seconds=30),
        ),
        _codex_response_item(
            {
                "type": "custom_tool_call_output",
                "call_id": "outer-unknown",
                "output": "passed without structured terminal metadata",
            },
            BASE_TIME + timedelta(seconds=31),
        ),
    ]
    _write_jsonl(transcript, prefix)
    session = _session("codex", transcript)

    await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)
    _append_jsonl(transcript, suffix)
    incremental = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)
    assert parse_counts == [len(prefix), len(suffix)]

    clear_evidence_snapshots()
    full = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)

    assert incremental == full
    assert [(run.command, run.outcome, run.exit_code) for run in incremental.validation_runs] == [
        ("uv run pytest tests/tasks", "success", 0),
        ("pytest", "unknown", None),
    ]
    assert [(edit.path, edit.tool_name) for edit in incremental.edits] == [
        ("src/changed.py", "apply_patch")
    ]
    assert incremental.degraded_capabilities != ()


async def test_truncated_transcript_falls_back_to_a_full_parse(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    transcript = tmp_path / "claude.jsonl"
    initial = [
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_a.py",
            call_id="run-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "passed"},
        ),
        *_claude_tool_pair(
            command="uv run ruff check src/",
            call_id="run-2",
            start=BASE_TIME + timedelta(seconds=5),
            result={"exit_code": 0, "stdout": ""},
        ),
    ]
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)

    await _derive(session, BASE_TIME, set(), tmp_path)

    truncated = initial[:2]
    _write_jsonl(transcript, truncated)
    evidence = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(initial), len(truncated)]
    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py"
    ]
    stored = _stored_snapshot(session, tmp_path)
    assert stored is not None
    assert stored.watermark == transcript.stat().st_size


async def test_rewritten_transcript_fails_the_tail_check_and_reparses(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """A rotated file that is long enough still fails the checksum at the watermark."""
    transcript = tmp_path / "claude.jsonl"
    initial = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, initial)
    session = _session("claude", transcript)

    await _derive(session, BASE_TIME, set(), tmp_path)
    old_size = transcript.stat().st_size

    replacement = [
        *_claude_tool_pair(
            command="uv run mypy src/",
            call_id="rotated-1",
            start=BASE_TIME + timedelta(seconds=10),
            result={"exit_code": 1, "stdout": "Found 2 errors in 1 file"},
            is_error=True,
        ),
        *_claude_tool_pair(
            command="uv run ruff check src/",
            call_id="rotated-2",
            start=BASE_TIME + timedelta(seconds=20),
            result={"exit_code": 0, "stdout": ""},
        ),
    ]
    _write_jsonl(transcript, replacement)
    assert transcript.stat().st_size >= old_size

    evidence = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [len(initial), len(replacement)]
    assert [run.command for run in evidence.validation_runs] == [
        "uv run mypy src/",
        "uv run ruff check src/",
    ]


async def test_archived_transcript_falls_back_to_a_full_parse(
    tmp_path: Path,
    parse_counts: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.transcript_evidence.find_transcript_on_disk",
        lambda *_args, **_kwargs: None,
    )
    transcript = tmp_path / "claude.jsonl"
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    records = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    _write_jsonl(transcript, records)
    session = _session("claude", transcript)

    live = await _derive(session, BASE_TIME, set(), tmp_path, archive_dir=str(archive_dir))

    transcript.unlink()
    archive = archive_dir / f"{session.external_id}.jsonl.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")

    archived = await _derive(session, BASE_TIME, set(), tmp_path, archive_dir=str(archive_dir))

    # The archive was parsed in full, not served from the live file's watermark.
    assert parse_counts == [len(records), len(records)]
    assert archived.validation_runs == live.validation_runs
    assert archived.attempted_paths[-1] == str(archive)
    stored = _stored_snapshot(session, tmp_path)
    assert stored is not None
    assert stored.transcript_path == str(transcript)


async def test_changed_task_files_bypass_the_snapshot(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """Edits filtered out of the cached prefix reappear when the task set grows."""
    transcript = tmp_path / "claude.jsonl"
    records = [
        _claude_edit(
            str(tmp_path / "src" / "a.py"),
            call_id="edit-a",
            at=BASE_TIME + timedelta(seconds=1),
        ),
        _claude_edit(
            str(tmp_path / "src" / "b.py"),
            call_id="edit-b",
            at=BASE_TIME + timedelta(seconds=2),
        ),
    ]
    _write_jsonl(transcript, records)
    session = _session("claude", transcript)

    narrow = await _derive(session, BASE_TIME, {"src/a.py"}, tmp_path)
    assert [edit.path for edit in narrow.edits] == ["src/a.py"]

    wide = await _derive(session, BASE_TIME, {"src/a.py", "src/b.py"}, tmp_path)

    assert parse_counts == [len(records), len(records)]
    assert [edit.path for edit in wide.edits] == ["src/a.py", "src/b.py"]


async def test_partial_trailing_line_is_parsed_but_never_persisted(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """A record still missing its newline is served once and never double-counted."""
    transcript = tmp_path / "claude.jsonl"
    pair = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_a.py",
        call_id="run-1",
        start=BASE_TIME,
        result={"exit_code": 0, "stdout": "passed"},
    )
    transcript.write_text(
        json.dumps(pair[0]) + "\n" + json.dumps(pair[1]),  # no trailing newline
        encoding="utf-8",
    )
    session = _session("claude", transcript)

    first = await _derive(session, BASE_TIME, set(), tmp_path)
    assert [run.outcome for run in first.validation_runs] == ["success"]
    assert _stored_snapshot(session, tmp_path) is None

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    _append_jsonl(
        transcript,
        _claude_tool_pair(
            command="uv run ruff check src/",
            call_id="run-2",
            start=BASE_TIME + timedelta(seconds=10),
            result={"exit_code": 0, "stdout": ""},
        ),
    )

    second = await _derive(session, BASE_TIME, set(), tmp_path)

    assert parse_counts == [2, 4]
    assert [run.command for run in second.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py",
        "uv run ruff check src/",
    ]
    stored = _stored_snapshot(session, tmp_path)
    assert stored is not None
    assert stored.watermark == transcript.stat().st_size


def test_snapshot_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_evidence_cache, "_DISK_SNAPSHOT_LIMIT", 3)
    base = transcript_evidence_snapshots.EvidenceSnapshot(
        fingerprint="f",
        transcript_path="/tmp/x.jsonl",
        watermark=0,
        tail_len=0,
        tail_sha256="",
        parser_state={},
        pending={},
        order=0,
        runs=(),
        edits=(),
        degraded=(),
    )
    # New fingerprints of one session share the existing global eviction bound.
    for index in range(6):
        transcript_evidence_snapshots.store_durable_snapshot(f"session:{index}", base)

    assert (
        len(list(transcript_evidence_cache._snapshot_path("session:5").parent.glob("*.json"))) == 3
    )
    assert transcript_evidence_snapshots.load_durable_snapshot("session:0") is None
    assert transcript_evidence_snapshots.load_durable_snapshot("session:5") == base


_RECALL = "d1965a04eb04"


def _long_session_records(tmp_path: Path) -> list[dict[str, Any]]:
    """Validation reds and greens among shell commands, then an rtk recall receipt."""
    noise = "line of shell output\n" * 200
    steps: list[tuple[str, Any, bool]] = [
        (
            "uv run ruff check src/ && uv run pytest tests/tasks/test_a.py -q",
            {"exit_code": 1, "stdout": "tests/tasks/test_a.py:3: AssertionError\n1 failed"},
            True,
        ),
        ("git status", {"exit_code": 0, "stdout": noise}, False),
        (
            "uv run pytest tests/tasks/test_b.py -q",
            {
                "exit_code": 1,
                "stdout": f"Pytest: 0 passed, 1 failed\n[full output: rtk recall {_RECALL}]",
            },
            True,
        ),
        ("git diff", {"exit_code": 0, "stdout": noise}, False),
        ("uv run pytest tests/tasks/test_a.py -q", {"exit_code": 0, "stdout": "4 passed"}, False),
        (
            f"rtk recall {_RECALL}",
            "tests/tasks/test_b.py:7: AssertionError: assert False\n1 failed in 0.01s",
            False,
        ),
        ("ls src", {"exit_code": 0, "stdout": noise}, False),
    ]
    records: list[dict[str, Any]] = [
        _claude_edit(
            str(tmp_path / "src" / "changed.py"),
            call_id="edit-1",
            at=BASE_TIME + timedelta(seconds=1),
        )
    ]
    for index, (command, result, is_error) in enumerate(steps):
        records.extend(
            _claude_tool_pair(
                command=command,
                call_id=f"step-{index}",
                start=BASE_TIME + timedelta(seconds=10 * (index + 1)),
                result=result,
                is_error=is_error,
            )
        )
    return records


def _derive_as_legacy(monkeypatch: pytest.MonkeyPatch, *, trim: bool) -> None:
    """Derive inline, keeping review-only output as snapshots written before #23256 did."""

    async def run_inline(function: Any, /, *args: Any) -> Any:
        return function(*args)

    # The pool's worker processes would not see the patched module.
    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_inline)
    monkeypatch.setattr(
        transcript_evidence,
        "_retained_output",
        lambda command, segments, output, output_truncated: (output, output_truncated),
    )
    if not trim:
        monkeypatch.setattr(transcript_evidence, "_drop_settled_command_output", lambda runs: runs)


async def test_resumed_snapshot_sheds_settled_command_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy snapshot loses settled shell output on resume; validation output stays."""
    transcript = tmp_path / "claude.jsonl"
    records = _long_session_records(tmp_path)
    session = _session("claude", transcript)
    _write_jsonl(transcript, records[:7])
    with monkeypatch.context() as legacy:
        _derive_as_legacy(legacy, trim=False)
        await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)
    legacy_snapshot = _stored_snapshot(session, tmp_path, task_files={"src/changed.py"})
    assert legacy_snapshot is not None
    assert next(run for run in legacy_snapshot.runs if run.command == "git status").output

    _append_jsonl(transcript, records[7:13])
    resumed = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)
    snapshot = _stored_snapshot(session, tmp_path, task_files={"src/changed.py"})

    assert snapshot is not None
    assert snapshot.parsed_from_offset > 0
    *settled, latest = [run for run in snapshot.runs if not run.categories]
    assert [run.command for run in settled] == ["git status", "git diff"]
    assert all(run.output is None for run in settled)
    # The newest run keeps its output: the Codex wrapper dedupe compares against it.
    assert latest.command == f"rtk recall {_RECALL}"
    assert latest.output
    assert all(run.output for run in snapshot.runs if run.categories)
    recalled = next(run for run in resumed.validation_runs if "test_b.py" in run.command)
    assert recalled.output_recovered_from == f"rtk recall {_RECALL}"

    _append_jsonl(transcript, records[13:])
    later = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)

    assert [run.output for run in later.command_runs] == [None, None, None, None]


async def test_dropping_settled_command_output_leaves_gate_findings_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop gate and the close gate decide the same with and without the trim."""
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _long_session_records(tmp_path))
    session = _session("claude", transcript)

    def findings(evidence: TranscriptEvidence) -> tuple[object, ...]:
        unresolved = unresolved_validation_failures(
            evidence.validation_runs, owner_handoff=False, project_path=str(tmp_path)
        )
        close = evaluate_validation_commands(
            task_category="code",
            evidence=evidence,
            has_attributed_edits=True,
            changed_paths=("src/changed.py",),
        )
        return (
            [run.command for run in unresolved],
            [sorted(_reported_failure_paths(run)) for run in unresolved],
            close,
        )

    derived: dict[bool, TranscriptEvidence] = {}
    for trim in (True, False):
        clear_evidence_snapshots()
        transcript_evidence_cache.clear_snapshots()
        with monkeypatch.context() as legacy:
            _derive_as_legacy(legacy, trim=trim)
            derived[trim] = await _derive(session, BASE_TIME, {"src/changed.py"}, tmp_path)

    assert any(run.output for run in derived[False].command_runs[:-1])
    assert not any(run.output for run in derived[True].command_runs[:-1])
    assert findings(derived[True]) == findings(derived[False])
    # The compound red's lint segment has no green; test_b's red has none either.
    assert findings(derived[True])[0] == [
        "uv run ruff check src/ && uv run pytest tests/tasks/test_a.py -q",
        "uv run pytest tests/tasks/test_b.py -q",
    ]
