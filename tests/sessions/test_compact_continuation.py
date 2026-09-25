"""Tests for set_handoff continuation marker delivery."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, IdleDetector
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_shutdown import _settle_finalizers_under_cancellation
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    _continuation_pane,
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
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.key_bytes import tmux_key_name
from gobby.terminals.pane_io import RuntimePaneIO, TmuxPaneIO
from gobby.terminals.runtime import Delivered, SnapshotMode
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
#: An empty Codex composer, rule-delimited, so the submit ladder can read one back.
_EMPTY_CODEX_COMPOSER = "\n".join(("output", "─" * 20, "›", "─" * 20, "  codex  12%"))
_CODEX_READ = IdleDetector(BundledDetectionRegistry(), "codex").composer_read
_ENTER = ("%12", "Enter", False)


@pytest.fixture(autouse=True)
def _no_enter_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap before the Enter is live-CLI timing, not something these tests wait on."""
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr(
        "gobby.sessions.compact_continuation._composer_reader",
        lambda _db, source: _CODEX_READ if source == "codex" else None,
    )


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
    #: A readable, empty composer by default -- the steady state of a pane whose
    #: text submitted. Tests that exercise an unclassifiable frame set this None.
    composer_text: str | None = _EMPTY_CODEX_COMPOSER

    def __init__(self) -> None:
        self.sent_keys: list[tuple[str, str, bool]] = []
        self.composer_modes: list[SnapshotMode] = []

    async def list_panes(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                pane_id="%12",
                session_name="codex-seat",
                pane_dead=False,
                pane_command="codex",
            )
        ]

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
        if literal and not text.endswith("\n"):
            self.composer_text = "\n".join(
                ("output", "─" * 20, f"› {text}", "─" * 20, "  codex  12%")
            )
        elif text == "Enter":
            self.composer_text = _EMPTY_CODEX_COMPOSER
        return True

    async def dispatch_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        return await self.send_keys(pane_id, text, literal=literal)

    async def snapshot_lines(
        self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        if lines == COMPOSER_PROBE_LINES:
            self.composer_modes.append(mode)
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
            "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
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

    # The write carries the newline; the Enter that submits a paste follows as its own
    # key, and the prompt itself was written exactly once.
    assert tmux.sent_keys[-2:] == [("%12", f"{prompt}\n", True), _ENTER]
    assert sum(1 for _pane, _text, literal in tmux.sent_keys if literal) == 1
    assert not _HANDOFF_COMPACT_CONTINUATION_TASKS


@pytest.mark.asyncio
@pytest.mark.parametrize("from_worker", [False, True], ids=["event-loop", "mcp-worker"])
async def test_shutdown_stops_readiness_watcher_and_preserves_pending_marker(
    session_db: HubDatabase, from_worker: bool
) -> None:
    snapshot_started = asyncio.Event()
    snapshot_cancelled = asyncio.Event()

    class WaitingTmux(_FakeTmux):
        async def snapshot_lines(
            self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
        ) -> str | None:
            snapshot_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                snapshot_cancelled.set()
            return None

    tmux = WaitingTmux()
    assert mark_handoff_compact_continuation_pending(session_db, SESSION_ID)
    loop = asyncio.get_running_loop()

    def schedule() -> bool:
        return schedule_codex_handoff_compact_continuation_readiness(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
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
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
            attempt_id="current-attempt",
        )

    assert tmux.sent_keys == [*_CODEX_DRAIN, ("%12", prompt, True), _ENTER]
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
            pane=TmuxPaneIO(UnexpectedTmux(), "%12"),
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
        pane=TmuxPaneIO(tmux, "%12"),
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
            pane=TmuxPaneIO(MissingPaneTmux(), "%12"),
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
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Compacting conversation",
        poll_seconds=0,
    )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
    assert [(m.content, m.message_type) for m in queued] == [(prompt, "handoff_continuation")]


@pytest.mark.asyncio
async def test_codex_exit_before_pull_prompt_uses_durable_fallback(
    session_db: HubDatabase,
) -> None:
    prompt = "Call `get_handoff()` after compaction."
    mark_handoff_compact_continuation_pending(session_db, SESSION_ID, prompt=prompt)

    class ExitedTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return "• Context compacted"

        async def list_panes(self) -> list[SimpleNamespace]:
            panes = await super().list_panes()
            panes[0].pane_command = "zsh"
            return panes

    tmux = ExitedTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Compacting conversation",
        poll_seconds=0,
    )

    assert tmux.sent_keys == []
    queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
    assert [(m.content, m.message_type) for m in queued] == [(prompt, "handoff_continuation")]


@pytest.mark.asyncio
async def test_codex_exit_during_pull_prompt_write_never_sends_shell_enter(
    session_db: HubDatabase,
) -> None:
    prompt = "Call `get_handoff()` after compaction."
    mark_handoff_compact_continuation_pending(session_db, SESSION_ID, prompt=prompt)

    class ExitingTmux(_FakeTmux):
        pane_command = "codex"

        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return "• Context compacted"

        async def list_panes(self) -> list[SimpleNamespace]:
            panes = await super().list_panes()
            panes[0].pane_command = self.pane_command
            return panes

        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            sent = await super().send_keys(pane_id, text, literal=literal)
            if literal and text == prompt:
                self.pane_command = "zsh"
                self.composer_text = f"josh % {prompt}"
            elif text == "C-u":
                self.composer_text = "josh % "
            return sent

    tmux = ExitingTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Compacting conversation",
        poll_seconds=0,
    )

    assert ("%12", prompt, True) in tmux.sent_keys
    assert _ENTER not in tmux.sent_keys
    assert tmux.composer_text == f"josh % {prompt}"
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
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [*_CODEX_DRAIN, ("%12", prompt, True), _ENTER]


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
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [*_CODEX_DRAIN, ("%12", prompt, True), _ENTER]


def test_codex_readiness_rejects_missing_baseline(session_db: HubDatabase) -> None:
    assert not schedule_codex_handoff_compact_continuation_readiness(
        session_db,
        pane=TmuxPaneIO(_FakeTmux(), "%12"),
        pending_session_id=SESSION_ID,
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
_CLAUDE_DRAIN = [("%12", tmux_key_name(key), False) for key in composer_clear_sequence("claude")]


def _claude_frame(row: str) -> str:
    rule = "─" * 20
    return f"⏺ done\n{rule}\n{row}\n{rule}\n   Fable 5.1  12%\n"


class _StickyComposerTmux(_FakeTmux):
    """Tmux fake whose composer keeps the pull prompt until enough Enters land.

    Zero releases on the write's own newline; one models a CLI that held that
    newline behind a paste review gate and wants a bare Enter. ``None`` never
    submits: the reported failure, where every write reports Delivered and the
    prompt stays on screen.
    """

    def __init__(
        self, releases_after_enters: int | None = None, prompt: str = _PULL_PROMPT
    ) -> None:
        super().__init__()
        self.releases_after_enters = releases_after_enters
        self.prompt = prompt

    @property
    def enters(self) -> int:
        return sum(1 for _pane, key, literal in self.sent_keys if key == "Enter" and not literal)

    @property
    def typed(self) -> list[str]:
        return [text for _pane, text, literal in self.sent_keys if literal]

    async def snapshot_lines(
        self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        if lines != COMPOSER_PROBE_LINES:
            return await super().snapshot_lines(pane_id, lines, mode=mode)
        self.composer_modes.append(mode)
        released = (
            self.releases_after_enters is not None and self.enters >= self.releases_after_enters
        )
        return _claude_frame("❯\xa0" if released else f"❯ {self.prompt}")


async def _send_pull_prompt(
    tmux: _FakeTmux, *, on_send_failure: Callable[[], None] | None = None
) -> bool:
    with (
        patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
        patch("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0),
    ):
        return await _send_handoff_compact_continuation(
            TmuxPaneIO(tmux, "%12"),
            _PULL_PROMPT,
            SESSION_ID,
            delay_seconds=0,
            cli_source="claude",
            on_send_failure=on_send_failure,
            composer_read=_CLAUDE_READ,
        )


class TestPullPromptFallback:
    """The pull prompt survives a failed send and never submits an operator draft."""

    @pytest.mark.asyncio
    async def test_a_clean_submit_is_one_write_and_one_enter(self) -> None:
        tmux = _StickyComposerTmux(releases_after_enters=0)

        assert await _send_pull_prompt(tmux) is True
        assert tmux.enters == 1
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]
        assert tmux.composer_modes == ["ansi"]

    @pytest.mark.asyncio
    async def test_a_paste_that_kept_its_newline_is_submitted_by_the_enter(self) -> None:
        tmux = _StickyComposerTmux(releases_after_enters=1)

        assert await _send_pull_prompt(tmux) is True
        assert tmux.enters == 1
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "composer_text",
        [
            _claude_frame("❯ the operator typed this"),
            _claude_frame("\x1b[39m❯\xa0\x1b[2mrun\x1b[0m \x1b[2mthe tests\x1b[0m"),
        ],
    )
    async def test_a_composer_that_is_not_our_prompt_counts_as_submitted(
        self, composer_text: str
    ) -> None:
        """A foreign draft is a positive read that our prompt went in: no retype."""
        tmux = _FakeTmux()
        tmux.composer_text = composer_text

        assert await _send_pull_prompt(tmux) is True
        assert [text for _p, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
        assert sum(1 for _p, key, literal in tmux.sent_keys if key == "Enter" and not literal) == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_composer_after_the_enter_is_trusted(self) -> None:
        """A frame we cannot classify after the Enter is not a failure.

        The write and the Enter were both delivered; retyping would queue the prompt
        twice, and failing would deliver it a second time through the durable
        fallback. What stranded the live prompts (gobby#22550) was skipping the
        Enter, not trusting the frame after it.
        """
        tmux = _FakeTmux()
        tmux.composer_text = None
        failures: list[int] = []

        assert await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is True
        assert failures == []
        assert [text for _p, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
        assert sum(1 for _p, key, literal in tmux.sent_keys if key == "Enter" and not literal) == 1

    @pytest.mark.asyncio
    async def test_a_prompt_that_never_leaves_is_drained_then_reported(self) -> None:
        tmux = _StickyComposerTmux()
        failures: list[int] = []

        assert await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is False
        assert failures == [0]
        # The held draft gets one bare-Enter retry, without a retype.
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]
        assert tmux.enters == 2
        # The draft is ours, so it is drained before the durable fallback delivers it.
        assert tmux.sent_keys[-len(_CLAUDE_DRAIN) :] == _CLAUDE_DRAIN

    @pytest.mark.asyncio
    async def test_an_unsubmitted_prompt_queues_itself_exactly_once(
        self, session_db: HubDatabase
    ) -> None:
        prompt = build_handoff_continue_prompt()
        mark_handoff_compact_continuation_pending(
            session_db, SESSION_ID, prompt=prompt, attempt_id="attempt-11"
        )
        session = SimpleNamespace(
            id=SESSION_ID, source="claude", terminal_context={"tmux_pane": "%12"}
        )
        tmux = _StickyComposerTmux(prompt=prompt)

        with (
            patch(
                "gobby.sessions.compact_continuation.manager_for_terminal_context",
                return_value=tmux,
            ),
            patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
            patch(
                "gobby.sessions.compact_continuation._composer_reader",
                return_value=_CLAUDE_READ,
            ),
        ):
            assert consume_and_schedule_handoff_compact_continuation(
                session_db, pending_session_id=SESSION_ID, target_session=session
            )
            await asyncio.gather(*_HANDOFF_COMPACT_CONTINUATION_TASKS)

        queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
        assert [m.content for m in queued] == [prompt]

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


async def test_schedule_continuation_resolves_native_terminal_without_tmux() -> None:
    """A gclient-hosted successor has no tmux identity; its live terminals row routes the pull."""
    session = SimpleNamespace(
        id=SESSION_ID,
        source="claude",
        terminal_context={"gobby_terminal_id": "term-1", "tmux_pane": None, "tmux_session": None},
    )
    prompt = "Continue the task."
    terminal = SimpleNamespace(id="term-1", backend="native")
    writes: list[tuple[object, ...]] = []

    class NativeRuntime:
        async def write_key(self, _terminal: object, key: str) -> Delivered:
            writes.append(("key", key))
            return Delivered()

        async def write_text(self, _terminal: object, text: str, submit: bool) -> Delivered:
            writes.append(("text", text, submit))
            return Delivered()

    runtime = NativeRuntime()
    terminal_manager = SimpleNamespace(
        get_live_for_session=lambda session_id: terminal if session_id == SESSION_ID else None
    )
    registry = SimpleNamespace(resolve=lambda backend: runtime if backend == "native" else None)

    # Without the runtime collaborators only a tmux target can be typed into.
    assert not schedule_handoff_compact_continuation(session, prompt, delay_seconds=0)

    with patch(
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    ):
        assert schedule_handoff_compact_continuation(
            session,
            prompt,
            delay_seconds=0,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=registry,
        )
        task = next(iter(_HANDOFF_COMPACT_CONTINUATION_TASKS))
        await task
        await drain_asyncio_tasks()

    assert writes.index(("text", prompt, True)) < writes.index(("key", "enter"))
    assert writes[-1] == ("key", "enter")
    assert not _HANDOFF_COMPACT_CONTINUATION_TASKS


def test_continuation_pane_uses_unbound_gterm_named_by_context() -> None:
    """Compact continuation uses the gterm row named by context when the session is unbound."""
    terminal_id = "11111111-1111-4111-8111-111111111111"
    terminal = SimpleNamespace(
        backend="native",
        id=terminal_id,
        state="live",
        project_id="proj-1",
        agent_run_id=None,
        session_id=None,
    )
    session = SimpleNamespace(
        id="session-1",
        project_id="proj-1",
        terminal_context={
            "gobby_terminal_id": terminal_id,
            "tmux_pane": None,
            "tmux_session": None,
        },
    )
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = None
    terminal_manager.get.return_value = terminal
    registry = MagicMock()

    pane = _continuation_pane(session, "session-1", terminal_manager, registry)

    assert isinstance(pane, RuntimePaneIO)
    assert (pane.backend, pane.target) == ("native", terminal_id)
