"""Close evidence waits for a late transcript flush before judging it (#22367)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence,
)
from gobby.storage.session_models import Session
from gobby.tasks.transcript_evidence_models import TranscriptEvidenceUnavailable
from gobby.tasks.transcript_sync import transcript_sync_point

pytestmark = pytest.mark.unit

_SUPPORT = "gobby.mcp_proxy.tools.tasks._close_evaluation_support"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"
SESSION_ID = "00000000-0000-0000-0000-000000022367"
BASE_TIME = datetime(2026, 9, 14, 0, 40, 4, tzinfo=UTC)
CRITERION_COMMAND = "uv run pytest tests/grok -q"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


def _stamp(offset_seconds: int) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def _tool_pair(command: str, call_id: str, *, start: int, end: int) -> list[dict[str, Any]]:
    """Grok's parsed transcript records for one completed terminal command."""
    return [
        {
            "timestamp": _stamp(start),
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": "run_terminal_command",
                "rawInput": {"command": command},
            },
        },
        {
            "timestamp": _stamp(end),
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
                "content": [{"type": "text", "text": "1 passed"}],
            },
        },
    ]


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _session(transcript_path: Path, *, source: str = "grok") -> Session:
    return Session(
        id=SESSION_ID,
        external_id="close-transcript-sync",
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript_path),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _context(session: Session) -> MagicMock:
    ctx = MagicMock()
    ctx.config = None
    ctx.session_task_manager.get_task_sessions.return_value = []
    ctx.session_manager.get.side_effect = lambda sid: session if sid == session.id else None
    return ctx


async def _derive(session: Session, repo_path: Path) -> Any:
    return await derive_close_transcript_evidence(
        _context(session),
        task_id="task-22367",
        owner_session_id=session.id,
        closing_session_id=session.id,
        owner_window_start=None,
        task_edited_files=set(),
        repo_path=str(repo_path),
    )


@pytest.mark.asyncio
async def test_late_grok_flush_still_credits_the_criterion_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-turn Grok run writes its transcript late; close must wait for it."""
    transcript = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    # The transcript holds only the run's first command ...
    _append_jsonl(transcript, _tool_pair("git status", "grok-1", start=0, end=1))
    # ... while the live sidecar already recorded a much later tool finishing.
    _append_jsonl(
        events,
        [
            {"ts": _stamp(0), "type": "turn_started"},
            {
                "ts": _stamp(600),
                "type": "tool_completed",
                "tool_name": "run_terminal_command",
                "outcome": "success",
            },
        ],
    )
    session = _session(transcript)

    real_sleep = asyncio.sleep
    flushes: list[float] = []

    async def flush(delay: float) -> None:
        if flushes:
            await real_sleep(delay)
            return
        flushes.append(delay)
        _append_jsonl(transcript, _tool_pair(CRITERION_COMMAND, "grok-2", start=595, end=600))

    monkeypatch.setattr(f"{_SUPPORT}.asyncio.sleep", flush)

    evidence = await _derive(session, tmp_path)

    assert flushes, "close returned before waiting for the transcript to catch up"
    assert [run.command for run in evidence.validation_runs] == [CRITERION_COMMAND]
    assert evidence.latest_record_at is not None


@pytest.mark.asyncio
async def test_transcript_that_never_catches_up_is_reported_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict is never rendered on a transcript known to be behind."""
    transcript = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    _append_jsonl(transcript, _tool_pair("git status", "grok-1", start=0, end=1))
    _append_jsonl(
        events,
        [
            {
                "ts": _stamp(600),
                "type": "tool_completed",
                "tool_name": "run_terminal_command",
                "outcome": "success",
            }
        ],
    )
    monkeypatch.setattr(f"{_SUPPORT}._TRANSCRIPT_CATCHUP_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(TranscriptEvidenceUnavailable) as excinfo:
        await _derive(_session(transcript), tmp_path)

    assert "still behind" in str(excinfo.value)
    assert excinfo.value.source == "grok"


@pytest.mark.asyncio
async def test_caught_up_transcript_is_derived_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    _append_jsonl(transcript, _tool_pair(CRITERION_COMMAND, "grok-1", start=595, end=600))
    _append_jsonl(
        events,
        [
            {
                "ts": _stamp(600),
                "type": "tool_completed",
                "tool_name": "run_terminal_command",
                "outcome": "success",
            }
        ],
    )

    async def fail_on_sleep(_delay: float) -> None:
        raise AssertionError("close waited although the transcript already caught up")

    monkeypatch.setattr(f"{_SUPPORT}.asyncio.sleep", fail_on_sleep)

    evidence = await _derive(_session(transcript), tmp_path)

    assert [run.command for run in evidence.validation_runs] == [CRITERION_COMMAND]


def test_sync_point_reports_the_newest_live_activity(tmp_path: Path) -> None:
    transcript = tmp_path / "updates.jsonl"
    transcript.touch()
    _append_jsonl(
        tmp_path / "events.jsonl",
        [
            {"ts": _stamp(10), "type": "tool_completed", "outcome": "success"},
            # Lifecycle chatter has no transcript counterpart and must not count.
            {"ts": _stamp(900), "type": "phase_changed", "phase": "thinking"},
            {"ts": _stamp(600), "type": "turn_ended", "outcome": "completed"},
        ],
    )

    assert transcript_sync_point(_session(transcript)) == BASE_TIME + timedelta(seconds=600)


@pytest.mark.parametrize("source", ["grok", "claude"])
def test_sync_point_is_none_without_a_readable_sidecar(tmp_path: Path, source: str) -> None:
    transcript = tmp_path / "updates.jsonl"
    transcript.touch()

    assert transcript_sync_point(_session(transcript, source=source)) is None
