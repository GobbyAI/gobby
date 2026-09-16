"""Tests for set_handoff continuation marker delivery."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, IdleDetector
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_shutdown import _settle_finalizers_under_cancellation
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    _continue_after_codex_compaction_ready,
    _count_codex_compact_ready_status_lines,
    _merge_session_variable,
    _pop_session_variable,
    _send_handoff_compact_continuation,
    clear_handoff_compact_continuation_pending,
    consume_and_schedule_handoff_compact_continuation,
    consume_handoff_compact_continuation_pending,
    mark_handoff_compact_continuation_pending,
    schedule_codex_handoff_compact_continuation_readiness,
    schedule_handoff_compact_continuation,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.transcript_cursor import CodexRolloutCursor, TranscriptObservationError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks
from tests.agents.detection_test_support import BundledDetectionRegistry

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_SESSION_ID = "00000000-0000-4000-8000-000000000002"
PROJECT_ID = "00000000-0000-4000-8000-000000000003"
TURN_ABORTED_RECORD = b'{"type":"event_msg","payload":{"type":"turn_aborted"}}\n'


@pytest.fixture
def session_db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "compact-continuation-test"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            SESSION_ID,
            "compact-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            PROJECT_ID,
        ),
    )
    return hub_db


# Gobby clears the composer before typing the pull prompt; Codex has no whole-buffer
# clear, so the drain sends kill-line/delete keys in passes.
_CODEX_DRAIN = [("%12", key, False) for key in ("C-u", "C-k", "BSpace", "DC")] * 8


def _append_bytes(path: Path, content: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(content)


def test_codex_rollout_cursor_detects_only_fresh_abort(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(TURN_ABORTED_RECORD)
    cursor = CodexRolloutCursor.at_eof(rollout)

    assert cursor.saw_fresh_turn_aborted() is False
    _append_bytes(rollout, b'{"type":"event_msg","payload":{"type":"token_count"}}\n')
    assert cursor.saw_fresh_turn_aborted() is False
    _append_bytes(rollout, TURN_ABORTED_RECORD)
    assert cursor.saw_fresh_turn_aborted() is True


def test_codex_rollout_cursor_handles_partial_and_malformed_records(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(TURN_ABORTED_RECORD.rstrip(b"\n"))
    cursor = CodexRolloutCursor.at_eof(rollout)

    _append_bytes(rollout, b"\nnot-json\n\xff\n" + TURN_ABORTED_RECORD[:24])
    assert cursor.saw_fresh_turn_aborted() is False
    _append_bytes(rollout, TURN_ABORTED_RECORD[24:])
    assert cursor.saw_fresh_turn_aborted() is True


def test_codex_rollout_cursor_rejects_replaced_transcript(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"session_meta"}\n')
    cursor = CodexRolloutCursor.at_eof(rollout)
    rollout.rename(tmp_path / "original-rollout.jsonl")
    rollout.write_bytes(TURN_ABORTED_RECORD)

    with pytest.raises(TranscriptObservationError, match="replaced"):
        cursor.saw_fresh_turn_aborted()


def test_codex_rollout_cursor_rejects_truncated_transcript(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"session_meta"}\n')
    cursor = CodexRolloutCursor.at_eof(rollout)
    rollout.write_bytes(b"")

    with pytest.raises(TranscriptObservationError, match="truncated"):
        cursor.saw_fresh_turn_aborted()


class _FakeTmux:
    composer_text: str | None = None

    def __init__(self) -> None:
        self.sent_keys: list[tuple[str, str, bool]] = []

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
        return True

    async def dispatch_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        return await self.send_keys(pane_id, text, literal=literal)

    async def snapshot_lines(self, pane_id: str, lines: int = 5) -> str | None:
        if lines == COMPOSER_PROBE_LINES:
            return self.composer_text
        capture = getattr(self, "capture_pane", None)
        if capture is None:
            return None
        captured = await capture(pane_id, lines=lines)
        return captured if isinstance(captured, str) else None


@pytest.mark.asyncio
async def test_scheduled_task_is_retained_and_multiline_prompt_is_sent_once() -> None:
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class BlockingTmux(_FakeTmux):
        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            self.sent_keys.append((pane_id, text, literal))
            if not literal:
                return True
            send_started.set()
            await release_send.wait()
            return True

    session = SimpleNamespace(id=SESSION_ID, terminal_context={"tmux_pane": "%12"})
    tmux = BlockingTmux()
    prompt = "Continue the task.\nPreserve the existing context."

    with (
        patch(
            "gobby.sessions.compact_continuation.manager_for_terminal_context",
            return_value=tmux,
        ),
        patch(
            "gobby.sessions.compact_continuation.HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
            0.0,
        ),
    ):
        assert schedule_handoff_compact_continuation(session, prompt, delay_seconds=0)
        await send_started.wait()

        assert len(_HANDOFF_COMPACT_CONTINUATION_TASKS) == 1
        assert tmux.sent_keys == [*_CODEX_DRAIN, ("%12", f"{prompt}\n", True)]
        task = next(iter(_HANDOFF_COMPACT_CONTINUATION_TASKS))

        release_send.set()
        await task
        await drain_asyncio_tasks()

    assert tmux.sent_keys[-1] == ("%12", "Enter", False)
    assert not _HANDOFF_COMPACT_CONTINUATION_TASKS


@pytest.mark.asyncio
@pytest.mark.parametrize("from_worker", [False, True], ids=["event-loop", "mcp-worker"])
async def test_shutdown_stops_readiness_watcher_and_preserves_pending_marker(
    session_db: HubDatabase, from_worker: bool
) -> None:
    snapshot_started = asyncio.Event()
    snapshot_cancelled = asyncio.Event()

    class WaitingTmux(_FakeTmux):
        async def snapshot_lines(self, pane_id: str, lines: int = 5) -> str | None:
            snapshot_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                snapshot_cancelled.set()
            return None

    tmux = WaitingTmux()
    session = SimpleNamespace(id=SESSION_ID, terminal_context={"tmux_pane": "%12"})
    assert mark_handoff_compact_continuation_pending(session_db, SESSION_ID)
    loop = asyncio.get_running_loop()

    def schedule() -> bool:
        return schedule_codex_handoff_compact_continuation_readiness(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=session,
            before_command="Compacting conversation",
            loop=loop,
        )

    with patch(
        "gobby.sessions.compact_continuation.manager_for_terminal_context", return_value=tmux
    ):
        scheduled = await asyncio.to_thread(schedule) if from_worker else schedule()
        assert scheduled
        await asyncio.wait_for(snapshot_started.wait(), timeout=2)
        # Exercise the daemon's cancellation-resistant finalizer, which runs
        # before its database pool closes even when graceful shutdown is cancelled.
        cancellation = asyncio.CancelledError()
        result = await _settle_finalizers_under_cancellation(
            cast(GobbyRunner, SimpleNamespace()), cancellation
        )

    assert result is cancellation
    assert snapshot_cancelled.is_set()
    assert not _HANDOFF_COMPACT_CONTINUATION_TASKS
    assert tmux.sent_keys == []
    assert consume_handoff_compact_continuation_pending(session_db, SESSION_ID) is not None


@pytest.mark.parametrize(
    "status, expected",
    [
        ("• Context compacted", 1),
        ("  • Context compacted · 2m 03s  ", 1),
        ("• Context compacted · additional status", 1),
        ("Waiting until • Context compacted", 0),
        ("› • Context compacted · 2m 03s", 0),
    ],
)
def test_codex_compaction_status_line(status: str, expected: int) -> None:
    assert _count_codex_compact_ready_status_lines(status) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["• Context compacted", "• Context compacted · 2m 03s"])
async def test_codex_waits_for_fresh_compaction_marker_before_continuing(
    session_db: HubDatabase,
    status: str,
) -> None:
    prompt = "Continue the claimed task."
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        prompt=prompt,
        attempt_id="current-attempt",
    )
    before_command = f"Earlier output\n{status}\n›"

    class ReadinessTmux(_FakeTmux):
        def __init__(self) -> None:
            super().__init__()
            self.outputs = iter(
                [
                    before_command,
                    f"{before_command}\nCompacting conversation",
                    f"{before_command}\n{status}\n›",
                ]
            )

        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            assert pane_id == "%12"
            assert lines == 100
            return next(self.outputs)

    tmux = ReadinessTmux()

    with patch(
        "gobby.sessions.compact_continuation.HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            tmux=tmux,
            target="%12",
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
            attempt_id="current-attempt",
        )

    # The paste is followed by a settle-tolerant second Enter (a no-op when the
    # first Enter already submitted).
    assert tmux.sent_keys == [
        *_CODEX_DRAIN,
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]
    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables


@pytest.mark.asyncio
async def test_codex_readiness_stops_when_attempt_marker_is_replaced(
    session_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="newer-attempt",
    )

    class UnexpectedTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            raise AssertionError("stale watcher must not inspect the pane")

    with caplog.at_level("WARNING"):
        await _continue_after_codex_compaction_ready(
            session_db,
            tmux=UnexpectedTmux(),
            target="%12",
            pending_session_id=SESSION_ID,
            before_command="Earlier output",
            poll_seconds=0,
            attempt_id="older-attempt",
            fresh_seconds=1,
        )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    marker = variables[HANDOFF_COMPACT_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert "Timed out waiting for Codex compact readiness" not in caplog.text


@pytest.mark.asyncio
async def test_codex_readiness_does_not_take_marker_replaced_during_capture(
    session_db: HubDatabase,
) -> None:
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="older-attempt",
    )

    class RacingTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            mark_handoff_compact_continuation_pending(
                session_db,
                SESSION_ID,
                attempt_id="newer-attempt",
            )
            return "Earlier output\n• Context compacted\n›"

    tmux = RacingTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        tmux=tmux,
        target="%12",
        pending_session_id=SESSION_ID,
        before_command="Earlier output",
        poll_seconds=0,
        attempt_id="older-attempt",
        fresh_seconds=1,
    )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    marker = variables[HANDOFF_COMPACT_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert tmux.sent_keys == []


@pytest.mark.asyncio
async def test_codex_readiness_stops_when_tmux_pane_disappears(
    session_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="current-attempt",
    )

    class MissingPaneTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            raise RuntimeError("tmux pane is gone")

    with caplog.at_level("WARNING"):
        await _continue_after_codex_compaction_ready(
            session_db,
            tmux=MissingPaneTmux(),
            target="%12",
            pending_session_id=SESSION_ID,
            before_command="Earlier output",
            poll_seconds=0,
            attempt_id="current-attempt",
            fresh_seconds=1,
        )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    marker = variables[HANDOFF_COMPACT_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "current-attempt"
    assert "Timed out waiting for Codex compact readiness" not in caplog.text


@pytest.mark.asyncio
async def test_codex_send_failure_queues_the_pull_prompt_for_the_next_turn(
    session_db: HubDatabase,
) -> None:
    """A restored marker has no consumer left, so the prompt rides the hook piggyback."""
    prompt = "Continue the claimed task."
    mark_handoff_compact_continuation_pending(session_db, SESSION_ID, prompt=prompt)

    class FailingTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return "• Context compacted"

        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            self.sent_keys.append((pane_id, text, literal))
            return False

    tmux = FailingTmux()

    await _continue_after_codex_compaction_ready(
        session_db,
        tmux=tmux,
        target="%12",
        pending_session_id=SESSION_ID,
        before_command="Compacting conversation",
        poll_seconds=0,
    )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
    assert [(m.content, m.message_type) for m in queued] == [(prompt, "handoff_continuation")]


@pytest.mark.asyncio
async def test_codex_detects_fresh_marker_when_old_marker_scrolls_out(
    session_db: HubDatabase,
) -> None:
    prompt = "Continue the claimed task."
    mark_handoff_compact_continuation_pending(session_db, SESSION_ID, prompt=prompt)
    before_command = "old\n• Context compacted\nshared one\nshared two"

    class RollingTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            assert pane_id == "%12"
            assert lines == 100
            return "shared one\nshared two\n• Context compacted\n›"

    tmux = RollingTmux()

    with patch(
        "gobby.sessions.compact_continuation.HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            tmux=tmux,
            target="%12",
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [
        *_CODEX_DRAIN,
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]


@pytest.mark.asyncio
async def test_codex_ignores_compaction_marker_text_in_prose(
    session_db: HubDatabase,
) -> None:
    prompt = "Continue the claimed task."
    mark_handoff_compact_continuation_pending(session_db, SESSION_ID, prompt=prompt)
    before_command = "Earlier output\n›"

    class ProseTmux(_FakeTmux):
        def __init__(self) -> None:
            super().__init__()
            self.capture_count = 0

        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            assert pane_id == "%12"
            assert lines == 100
            self.capture_count += 1
            if self.capture_count == 1:
                return f"{before_command}\nWaiting until Context compacted appears."
            assert not self.sent_keys
            return f"{before_command}\n• Context compacted\n›"

    tmux = ProseTmux()

    with patch(
        "gobby.sessions.compact_continuation.HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            tmux=tmux,
            target="%12",
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [
        *_CODEX_DRAIN,
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]


def test_codex_readiness_rejects_missing_baseline(session_db: HubDatabase) -> None:
    session = SimpleNamespace(terminal_context={"tmux_pane": "%12"})

    assert not schedule_codex_handoff_compact_continuation_readiness(
        session_db,
        pending_session_id=SESSION_ID,
        target_session=session,
        before_command=None,
    )


def test_merge_session_variable_serializes_with_workflow_first_write(
    session_db: HubDatabase,
) -> None:
    manager = SessionVariableManager(session_db)
    barrier = threading.Barrier(3)

    def merge_compact_variable() -> None:
        barrier.wait()
        _merge_session_variable(session_db, SESSION_ID, "compact", True)

    def merge_workflow_variable() -> None:
        barrier.wait()
        manager.merge_variables(SESSION_ID, {"workflow": True})

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(merge_compact_variable),
            executor.submit(merge_workflow_variable),
        ]
        barrier.wait()
        for future in futures:
            future.result()

    assert manager.get_variables(SESSION_ID) == {"compact": True, "workflow": True}


def test_pop_session_variable_serializes_with_workflow_write(
    session_db: HubDatabase,
) -> None:
    manager = SessionVariableManager(session_db)
    manager.merge_variables(SESSION_ID, {"discard": True})
    barrier = threading.Barrier(3)

    def pop_compact_variable() -> bool:
        barrier.wait()
        return bool(_pop_session_variable(session_db, SESSION_ID, "discard"))

    def merge_workflow_variable() -> None:
        barrier.wait()
        manager.merge_variables(SESSION_ID, {"workflow": True})

    with ThreadPoolExecutor(max_workers=2) as executor:
        pop_future = executor.submit(pop_compact_variable)
        merge_future = executor.submit(merge_workflow_variable)
        barrier.wait()
        assert pop_future.result() is True
        merge_future.result()

    assert manager.get_variables(SESSION_ID) == {"workflow": True}


def test_pending_marker_stores_pull_prompt(session_db: HubDatabase) -> None:
    assert mark_handoff_compact_continuation_pending(session_db, SESSION_ID)

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert "get_handoff()" in variables[HANDOFF_COMPACT_CONTINUE_VARIABLE]["prompt"]


def test_clear_pending_marker_removes_only_matching_compact_attempt(
    session_db: HubDatabase,
) -> None:
    sv_mgr = SessionVariableManager(session_db)
    assert mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="first-attempt",
    )
    assert mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="newer-attempt",
    )

    assert not clear_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="first-attempt",
    )
    marker = sv_mgr.get_variables(SESSION_ID)[HANDOFF_COMPACT_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert clear_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="newer-attempt",
    )
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in sv_mgr.get_variables(SESSION_ID)


def test_pending_marker_expires_from_its_creation_time(session_db: HubDatabase) -> None:
    created_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        now=created_at,
    )

    prompt = consume_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        now=datetime(2026, 7, 28, 12, 0, 2, tzinfo=UTC),
        fresh_seconds=1,
    )

    assert prompt is None
    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables


def test_failed_schedule_restores_exact_pending_marker(session_db: HubDatabase) -> None:
    created_at = datetime.now(UTC).isoformat()
    sv_mgr = SessionVariableManager(session_db)
    payload = {
        "prompt": "continue exactly",
        "created_at": created_at,
        "summary_session_id": SOURCE_SESSION_ID,
    }
    sv_mgr.merge_variables(SESSION_ID, {HANDOFF_COMPACT_CONTINUE_VARIABLE: payload})

    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        return_value=False,
    ):
        scheduled = consume_and_schedule_handoff_compact_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(id=SESSION_ID),
        )

    assert scheduled is False
    assert sv_mgr.get_variables(SESSION_ID)[HANDOFF_COMPACT_CONTINUE_VARIABLE] == payload


def test_failed_schedule_does_not_replace_newer_pending_marker(session_db: HubDatabase) -> None:
    sv_mgr = SessionVariableManager(session_db)
    old_payload = {
        "prompt": "old prompt",
        "created_at": datetime.now(UTC).isoformat(),
    }
    new_payload = {
        "prompt": "new prompt",
        "created_at": datetime.now(UTC).isoformat(),
    }
    sv_mgr.merge_variables(SESSION_ID, {HANDOFF_COMPACT_CONTINUE_VARIABLE: old_payload})

    def fail_after_new_marker(*_args: object, **_kwargs: object) -> bool:
        sv_mgr.merge_variables(SESSION_ID, {HANDOFF_COMPACT_CONTINUE_VARIABLE: new_payload})
        return False

    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        side_effect=fail_after_new_marker,
    ):
        scheduled = consume_and_schedule_handoff_compact_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(id=SESSION_ID),
        )

    assert scheduled is False
    assert sv_mgr.get_variables(SESSION_ID)[HANDOFF_COMPACT_CONTINUE_VARIABLE] == new_payload


def test_in_place_compact_consumes_pending_on_same_terminal_row(
    session_db: HubDatabase,
) -> None:
    marked_id = "00000000-0000-4000-8000-000000000014"
    terminal_context = {
        "tmux_pane": "%11",
        "tmux_socket_path": "/tmp/tmux-compact-test",
        "parent_pid": 30234,
        "parent_create_time": 1786658058.615728,
    }
    session_db.execute(
        """
        UPDATE sessions
           SET terminal_context = %s::jsonb, session_type = 'terminal'
         WHERE id = %s
        """,
        (json.dumps(terminal_context), SESSION_ID),
    )
    session_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id,
            session_type, terminal_context
        )
        VALUES (%s, %s, %s, %s, %s, 'terminal', %s::jsonb)
        """,
        (
            marked_id,
            "compact-marked-row",
            "21000000-0000-4000-8000-000000000001",
            "grok",
            PROJECT_ID,
            json.dumps(terminal_context),
        ),
    )
    assert mark_handoff_compact_continuation_pending(session_db, marked_id)

    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        return_value=True,
    ) as mock_schedule:
        scheduled = consume_and_schedule_handoff_compact_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(
                id=SESSION_ID,
                terminal_context=terminal_context,
            ),
        )

    assert scheduled is True
    mock_schedule.assert_called_once()
    variables = SessionVariableManager(session_db).get_variables(marked_id)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables


def test_in_place_compact_does_not_steal_pending_from_other_terminal(
    session_db: HubDatabase,
) -> None:
    marked_id = "00000000-0000-4000-8000-000000000015"
    session_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id,
            session_type, terminal_context
        )
        VALUES (%s, %s, %s, %s, %s, 'terminal', %s::jsonb)
        """,
        (
            marked_id,
            "other-pane-marked-row",
            "21000000-0000-4000-8000-000000000001",
            "grok",
            PROJECT_ID,
            json.dumps(
                {
                    "tmux_pane": "%99",
                    "tmux_socket_path": "/tmp/tmux-compact-test",
                    "parent_pid": 99999,
                    "parent_create_time": 1.0,
                }
            ),
        ),
    )
    assert mark_handoff_compact_continuation_pending(session_db, marked_id)

    scheduled = consume_and_schedule_handoff_compact_continuation(
        session_db,
        pending_session_id=SESSION_ID,
        target_session=SimpleNamespace(
            id=SESSION_ID,
            terminal_context={
                "tmux_pane": "%11",
                "tmux_socket_path": "/tmp/tmux-compact-test",
                "parent_pid": 30234,
                "parent_create_time": 1786658058.615728,
            },
        ),
    )

    assert scheduled is False
    variables = SessionVariableManager(session_db).get_variables(marked_id)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE in variables


def test_reload_directive_normalized() -> None:
    """The typed trigger is one paste line and requires pull-only recovery."""
    prompt = build_handoff_continue_prompt()

    assert "get_handoff()" in prompt
    assert "set_handoff" in prompt
    assert "cancelled" in prompt
    assert "context pressure" in prompt
    assert "injected context" not in prompt
    assert "\n" not in prompt
    assert "tier" not in prompt
    assert "get_skill" not in prompt


_CLAUDE_READ = IdleDetector(BundledDetectionRegistry(), "claude").composer_read
_PULL_PROMPT = "Continue the claimed task by calling get_handoff first."


def _claude_frame(row: str) -> str:
    rule = "─" * 20
    return f"⏺ done\n{rule}\n{row}\n{rule}\n   Fable 5.1  12%\n"


class TestPullPromptFallback:
    """The pull prompt survives a failed send and never submits an operator draft."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("composer_text", "second_enter"),
        [
            (_claude_frame("❯\xa0"), False),
            (_claude_frame(f"❯ {_PULL_PROMPT}"), True),
            (_claude_frame("❯ the operator typed this"), False),
            (None, True),
        ],
    )
    async def test_follow_up_enter_is_gated_on_the_composer_read(
        self, composer_text: str | None, second_enter: bool
    ) -> None:
        tmux = _FakeTmux()
        tmux.composer_text = composer_text

        with patch(
            "gobby.sessions.compact_continuation.HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
            0.0,
        ):
            sent = await _send_handoff_compact_continuation(
                tmux,
                "%12",
                _PULL_PROMPT,
                SESSION_ID,
                delay_seconds=0,
                cli_source="claude",
                composer_read=_CLAUDE_READ,
            )

        assert sent is True
        assert (("%12", "Enter", False) in tmux.sent_keys) is second_enter

    @pytest.mark.asyncio
    async def test_scheduled_send_failure_queues_the_pull_prompt(
        self, session_db: HubDatabase
    ) -> None:
        prompt = "Continue the claimed task."
        mark_handoff_compact_continuation_pending(
            session_db, SESSION_ID, prompt=prompt, attempt_id="attempt-9"
        )

        session = SimpleNamespace(
            id=SESSION_ID, source="claude", terminal_context={"tmux_pane": "%12"}
        )
        with (
            patch(
                "gobby.sessions.compact_continuation.manager_for_terminal_context",
                return_value=_FakeTmux(),
            ),
            patch(
                "gobby.sessions.compact_continuation._type_handoff_compact_continuation",
                new=AsyncMock(return_value=False),
            ),
        ):
            scheduled = consume_and_schedule_handoff_compact_continuation(
                session_db, pending_session_id=SESSION_ID, target_session=session
            )
            await asyncio.gather(*_HANDOFF_COMPACT_CONTINUATION_TASKS)

        assert scheduled is True
        queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
        assert [m.content for m in queued] == [prompt]
        assert json.loads(queued[0].metadata_json or "{}") == {
            "attempt_id": "attempt-9",
            "compact_continuation": True,
        }
