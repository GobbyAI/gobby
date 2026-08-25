"""Incremental close-evidence derivation: watermarks, resume, and fallbacks."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.sessions.transcripts.base import RawLine
from gobby.tasks import transcript_evidence
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    clear_evidence_snapshots,
    derive_transcript_evidence,
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
    stored = transcript_evidence._load_snapshot(session.id)
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
    advanced = transcript_evidence._load_snapshot(session.id)
    assert advanced is not None
    assert advanced.watermark == transcript.stat().st_size
    assert advanced.watermark > stored.watermark

    assert [run.command for run in second.validation_runs] == [
        "uv run pytest tests/tasks/test_a.py",
        "uv run ruff check src/",
    ]
    assert second.validation_runs[: len(first.validation_runs)] == first.validation_runs
    assert second.edits == first.edits


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


async def test_codex_execution_chain_straddling_the_watermark_matches_full_parse(
    tmp_path: Path, parse_counts: list[int]
) -> None:
    """The Codex parser's own cross-line state survives the watermark."""
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
    stored = transcript_evidence._load_snapshot(session.id)
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
    stored = transcript_evidence._load_snapshot(session.id)
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
    assert transcript_evidence._load_snapshot(session.id) is None

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
    stored = transcript_evidence._load_snapshot(session.id)
    assert stored is not None
    assert stored.watermark == transcript.stat().st_size


def test_snapshot_store_is_bounded() -> None:
    base = transcript_evidence._EvidenceSnapshot(
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
    for index in range(transcript_evidence._SNAPSHOT_LIMIT + 3):
        transcript_evidence._store_snapshot(f"session-{index}", base)

    assert len(transcript_evidence._evidence_snapshots) == transcript_evidence._SNAPSHOT_LIMIT
    assert transcript_evidence._load_snapshot("session-0") is None
    assert transcript_evidence._load_snapshot("session-3") is base
