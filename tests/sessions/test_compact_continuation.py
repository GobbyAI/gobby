"""Tests for set_handoff continuation marker delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import textwrap
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead, IdleDetector
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_shutdown import _settle_finalizers_under_cancellation
from gobby.sessions import continuation_retry
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    _continuation_pane,
    _continue_after_codex_compaction_ready,
    _count_codex_compact_ready_status_lines,
    _send_handoff_compact_continuation,
    clear_handoff_compact_continuation_pending,
    consume_and_schedule_handoff_compact_continuation,
    consume_handoff_compact_continuation_pending,
    mark_handoff_compact_continuation_pending,
    persist_pull_prompt_message,
    schedule_codex_handoff_compact_continuation_readiness,
    schedule_handoff_compact_continuation,
)
from gobby.sessions.compact_continuation_store import _merge_session_variable, _pop_session_variable
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    build_handoff_continue_prompt,
    consume_pending_handoff,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.sessions.transcript_cursor import CodexRolloutCursor, TranscriptObservationError
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.key_bytes import tmux_key_name
from gobby.terminals.native_runtime import (
    NativeBatchFailure,
    NativeBatchResult,
    NativeBatchTarget,
    NativeTerminalRuntime,
)
from gobby.terminals.pane_io import RuntimePaneIO, SubmitResult, TmuxPaneIO
from gobby.terminals.runtime import Delivered, SnapshotMode
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks
from tests.agents.detection_test_support import BundledDetectionRegistry

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_SESSION_ID = "00000000-0000-4000-8000-000000000002"
PROJECT_ID = "00000000-0000-4000-8000-000000000003"
TURN_ABORTED_RECORD = b'{"type":"event_msg","payload":{"type":"turn_aborted"}}\n'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_landed,late_reread",
    [(False, False), (True, False), (True, True)],
    ids=["lost-submit", "late-before-agent", "late-composer-read"],
)
async def test_codex_unverified_continuation_recovers_without_duplicate(
    session_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, first_landed: bool, late_reread: bool
) -> None:
    attempt_id = "d" * 32
    prompt = "Call get_handoff for the completed compact and continue."
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacted", next_steps=["Continue"]),
        clear_session=False,
    )
    record_handoff_delivery(
        session_db,
        handoff_id=staged.handoff_record_id,
        attempt_id=attempt_id,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )
    mark_handoff_compact_continuation_pending(
        session_db, SESSION_ID, prompt=prompt, attempt_id=attempt_id
    )
    manager = SessionManager(session_db)
    manager.update_session_status(SESSION_ID, "awaiting_handoff")
    lifecycle = TurnLifecycleReducer(manager)
    accepted: list[str] = []

    class UnverifiedTmux(_FakeTmux):
        enters = 0
        draft = ""
        hook_seen = False

        async def snapshot_lines(
            self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
        ) -> str | None:
            if late_reread and self.enters == 2 and not self.hook_seen:
                lifecycle.begin_turn(SESSION_ID, TurnEvidence(source="hook"))
                self.hook_seen = True
            return await super().snapshot_lines(pane_id, lines, mode=mode)

        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return "• Context compacted\n›"

        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            result = await super().send_keys(pane_id, text, literal=literal)
            if literal:
                self.draft = text.strip()
            elif text == "Enter":
                self.enters += 1
                if self.enters == 1:
                    if first_landed:
                        accepted.append(self.draft)
                    # The first Enter's verification frame is unavailable.
                    self.composer_text = None
                    self.draft = ""
                elif self.draft:
                    accepted.append(self.draft)
                    self.draft = ""
                    lifecycle.begin_turn(SESSION_ID, TurnEvidence(source="hook"))
                    self.composer_text = _EMPTY_CODEX_COMPOSER
            return result

    tmux = UnverifiedTmux()
    checks: list[int | None] = []
    original_check = continuation_retry.await_before_agent

    async def check(
        db: HubDatabase, session_id: str, *, baseline_generation: int | None, **_kwargs: Any
    ) -> bool:
        checks.append(baseline_generation)
        tmux.composer_text = _EMPTY_CODEX_COMPOSER
        if first_landed and not late_reread and len(checks) == 1:
            # BEFORE_AGENT lands late, after the unverified screen read.
            lifecycle.begin_turn(SESSION_ID, TurnEvidence(source="hook"))
        return await original_check(
            db, session_id, baseline_generation=baseline_generation, timeout_seconds=0
        )

    monkeypatch.setattr(continuation_retry, "await_before_agent", check)
    monkeypatch.setattr("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr(
        "gobby.sessions.compact_continuation._composer_reader", lambda *_args: _CODEX_READ
    )
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Before /compact\n›",
        poll_seconds=0,
        attempt_id=attempt_id,
    )

    assert checks, "unverified submission never entered bounded lifecycle verification"
    assert accepted == [prompt]
    recipient = manager.get(SESSION_ID)
    assert recipient is not None and recipient.status == "active"
    typed = [text for _, text, literal in tmux.sent_keys if literal]
    assert typed == [f"{prompt}\n"] * (1 if first_landed else 2)
    assert tmux.enters == (2 if late_reread else 1 if first_landed else 3)
    assert InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID) == []
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in SessionVariableManager(
        session_db
    ).get_variables(SESSION_ID)
    receipt = session_db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries WHERE attempt_id = %s", (attempt_id,)
    )
    assert receipt is not None and receipt["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["awaiting_input", "awaiting_approval"])
@pytest.mark.parametrize("during_retry", [False, True], ids=["initial-wait", "wait-during-retry"])
@pytest.mark.parametrize("durable_wait", [False, True], ids=["projected-status", "durable-token"])
async def test_codex_continuation_never_types_into_interaction_wait(
    session_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    during_retry: bool,
    durable_wait: bool,
) -> None:
    manager = SessionManager(session_db)
    lifecycle = TurnLifecycleReducer(manager)
    lifecycle.begin_turn(SESSION_ID, TurnEvidence(source="hook"))
    manager.update_session_status(SESSION_ID, "awaiting_handoff")

    def protect_interaction() -> None:
        if durable_wait:
            lifecycle.enter_wait(
                SESSION_ID,
                kind="input" if status == "awaiting_input" else "approval",
                token="operator-interaction",
                evidence=TurnEvidence(source="hook"),
            )
            # The durable wait must protect writes even before status projection catches up.
            manager.update_session_status(SESSION_ID, "awaiting_handoff")
            assert lifecycle.get(SESSION_ID).waits
        else:
            manager.update_session_status(SESSION_ID, status)

    if not during_retry:
        protect_interaction()

    class UnverifiedTmux(_FakeTmux):
        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            result = await super().send_keys(pane_id, text, literal=literal)
            if text == "Enter" and not literal:
                self.composer_text = None
            return result

    tmux = UnverifiedTmux()
    original_check = continuation_retry.await_before_agent

    async def check(
        db: HubDatabase, session_id: str, *, baseline_generation: int | None, **_kwargs: Any
    ) -> bool:
        protect_interaction()
        tmux.composer_text = _EMPTY_CODEX_COMPOSER
        return await original_check(
            db, session_id, baseline_generation=baseline_generation, timeout_seconds=0
        )

    monkeypatch.setattr(continuation_retry, "await_before_agent", check)
    monkeypatch.setattr("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    failures: list[int] = []
    sent = await _send_handoff_compact_continuation(
        TmuxPaneIO(tmux, "%12"),
        _PULL_PROMPT,
        SESSION_ID,
        delay_seconds=0,
        cli_source="codex",
        composer_read=_CODEX_READ,
        db=session_db,
        on_send_failure=lambda: failures.append(1),
    )
    assert sent is False
    assert failures == [1]
    recipient = manager.get(SESSION_ID)
    assert recipient is not None
    assert recipient.status == ("awaiting_handoff" if durable_wait else status)
    assert tmux.sent_keys == ([("%12", f"{_PULL_PROMPT}\n", True), _ENTER] if during_retry else [])


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


#: An empty Codex composer, rule-delimited, so the submit ladder can read one back.
_EMPTY_CODEX_COMPOSER = "\n".join(("output", "─" * 20, "›", "─" * 20, "  codex  12%"))
_ENTER = ("%12", "Enter", False)


@pytest.fixture(autouse=True)
def _no_enter_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap before the Enter is live-CLI timing, not something these tests wait on."""
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)


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

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
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
            "gobby.sessions.compact_continuation._continuation_pane",
            return_value=TmuxPaneIO(tmux, "%12"),
        ),
        patch(
            "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
            0.0,
        ),
        patch(
            "gobby.sessions.compact_continuation._composer_reader",
            return_value=_CODEX_READ,
        ),
    ):
        assert schedule_handoff_compact_continuation(session, prompt, delay_seconds=0)
        await send_started.wait()

        assert len(_HANDOFF_COMPACT_CONTINUATION_TASKS) == 1
        assert tmux.sent_keys == [("%12", f"{prompt}\n", True)]
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
async def test_codex_waits_for_delivery_receipt_before_continuing(
    session_db: HubDatabase,
    status: str,
) -> None:
    prompt = "Continue the claimed task."
    attempt_id = "a" * 32
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacting", next_steps=["Continue"]),
        clear_session=False,
    )
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        prompt=prompt,
        attempt_id=attempt_id,
    )
    before_command = f"Earlier output\n{status}\n›"

    class ReadinessTmux(_FakeTmux):
        def __init__(self) -> None:
            super().__init__()
            self.capture_count = 0
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
            self.capture_count += 1
            if self.capture_count == 3:
                record_handoff_delivery(
                    session_db,
                    handoff_id=staged.handoff_record_id,
                    attempt_id=attempt_id,
                    boundary_kind="compact",
                    continuation_session_id=SESSION_ID,
                )
            return next(self.outputs)

    tmux = ReadinessTmux()

    with (
        patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
        patch(
            "gobby.sessions.compact_continuation._composer_reader",
            return_value=_CODEX_READ,
        ),
        patch(
            "gobby.sessions.continuation_retry.await_before_agent",
            AsyncMock(return_value=True),
        ),
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
            attempt_id=attempt_id,
        )

    # The confirmed-empty composer is not drained. Its empty read after Enter
    # proves the prompt left it.
    assert tmux.sent_keys == [("%12", f"{prompt}\n", True), _ENTER]
    assert tmux.capture_count == 3
    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["no_receipt", "new_gate", "new_marker"])
async def test_readiness_timeout_preserves_undelivered_or_replaced_attempt(
    session_db: HubDatabase,
    conflict: str,
) -> None:
    attempt_id = "a" * 32
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacted", next_steps=["Continue"]),
        clear_session=False,
    )
    if conflict != "no_receipt":
        record_handoff_delivery(
            session_db,
            handoff_id=staged.handoff_record_id,
            attempt_id=attempt_id,
            boundary_kind="compact",
            continuation_session_id=SESSION_ID,
        )
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        prompt="Call get_handoff",
        attempt_id="b" * 32 if conflict == "new_marker" else attempt_id,
    )
    variables = SessionVariableManager(session_db)
    variables.set_variable(
        SESSION_ID,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {
            "attempt_id": "b" * 32 if conflict == "new_gate" else attempt_id,
            "delivery_pending": True,
        },
    )
    before = variables.get_variables(SESSION_ID)
    tmux = _FakeTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Before /compact\n›",
        poll_seconds=0,
        attempt_id=attempt_id,
        fresh_seconds=-1,
    )
    assert variables.get_variables(SESSION_ID) == before
    assert tmux.sent_keys == []


@pytest.mark.asyncio
async def test_delivered_codex_readiness_timeout_releases_tools_for_recovery(
    session_db: HubDatabase,
) -> None:
    attempt_id = "a" * 32
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacted", next_steps=["Continue"]),
        clear_session=False,
    )
    record_handoff_delivery(
        session_db,
        handoff_id=staged.handoff_record_id,
        attempt_id=attempt_id,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )
    mark_handoff_compact_continuation_pending(
        session_db, SESSION_ID, prompt="Call get_handoff", attempt_id=attempt_id
    )
    variables = SessionVariableManager(session_db)
    variables.set_variable(
        SESSION_ID,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {"attempt_id": attempt_id, "delivery_pending": True},
    )
    tmux = _FakeTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Before /compact\n›",
        poll_seconds=0,
        attempt_id=attempt_id,
        fresh_seconds=-1,
    )

    result = variables.get_variables(SESSION_ID)
    gate = result[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["delivery_failed"] is True
    assert gate["delivery_pending"] is False
    assert gate["attempt_pending"] is False
    assert gate["error_code"] == "compact_unconfirmed"
    assert gate["readiness_unconfirmed"] is True
    assert result[HANDOFF_COMPACT_CONTINUE_VARIABLE]["attempt_id"] == attempt_id
    assert tmux.sent_keys == []
    recovered = consume_pending_handoff(session_db, SESSION_ID)
    assert recovered is not None
    assert recovered.attempt_id == attempt_id
    assert "Compacted" in recovered.markdown
    remaining = variables.get_variables(SESSION_ID)
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in remaining
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in remaining


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["awaiting_handoff", "active", "paused", "expired"])
@pytest.mark.parametrize("conflict", [None, "no_receipt", "new_gate", "new_marker"])
async def test_readiness_timeout_recovers_only_matching_awaiting_session(
    session_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    status: str,
    conflict: str | None,
) -> None:
    attempt_id = "a" * 32
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Recover me", next_steps=["Continue"]),
        clear_session=False,
    )
    if conflict != "no_receipt":
        record_handoff_delivery(
            session_db,
            handoff_id=staged.handoff_record_id,
            attempt_id=attempt_id,
            boundary_kind="compact",
            continuation_session_id=SESSION_ID,
        )
    mark_handoff_compact_continuation_pending(
        session_db,
        SESSION_ID,
        prompt="Call get_handoff",
        attempt_id="b" * 32 if conflict == "new_marker" else attempt_id,
    )
    variables = SessionVariableManager(session_db)
    variables.set_variable(
        SESSION_ID,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {"attempt_id": "b" * 32 if conflict == "new_gate" else attempt_id},
    )
    session_db.execute("UPDATE sessions SET status = %s WHERE id = %s", (status, SESSION_ID))
    before = variables.get_variables(SESSION_ID)
    tmux = _FakeTmux()

    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Before /compact\n›",
        poll_seconds=0,
        attempt_id=attempt_id,
        fresh_seconds=-1,
    )

    row = session_db.fetchone("SELECT status FROM sessions WHERE id = %s", (SESSION_ID,))
    assert row is not None
    recover = status == "awaiting_handoff" and conflict is None
    assert row["status"] == ("paused" if recover else status)
    after = variables.get_variables(SESSION_ID)
    assert after[HANDOFF_COMPACT_CONTINUE_VARIABLE] == before[HANDOFF_COMPACT_CONTINUE_VARIABLE]
    assert tmux.sent_keys == []
    if recover:
        assert "Released awaiting_handoff after Codex readiness timeout" in caplog.text
        recovered = consume_pending_handoff(session_db, SESSION_ID)
        assert recovered is not None
        assert recovered.attempt_id == attempt_id
        assert "Recover me" in recovered.markdown
        assert consume_pending_handoff(session_db, SESSION_ID) is None
    elif conflict is not None:
        assert after == before


@pytest.mark.parametrize("gate_present", [False, True])
@pytest.mark.asyncio
async def test_codex_delivered_boundary_continues_without_visible_status_marker(
    session_db: HubDatabase,
    gate_present: bool,
) -> None:
    attempt_id = "a" * 32
    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacted", next_steps=["Continue"]),
        clear_session=False,
    )
    record_handoff_delivery(
        session_db,
        handoff_id=staged.handoff_record_id,
        attempt_id=attempt_id,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )
    mark_handoff_compact_continuation_pending(
        session_db, SESSION_ID, prompt="Call get_handoff", attempt_id=attempt_id
    )
    variables = SessionVariableManager(session_db)
    variables.set_variable(
        SESSION_ID,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {"attempt_id": attempt_id, "delivery_pending": True} if gate_present else None,
    )

    class IdleTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return _EMPTY_CODEX_COMPOSER

    tmux = IdleTmux()
    with patch(
        "gobby.sessions.continuation_retry.await_before_agent",
        AsyncMock(return_value=True),
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=_EMPTY_CODEX_COMPOSER,
            poll_seconds=0.01,
            attempt_id=attempt_id,
            fresh_seconds=1,
        )

    assert sum(text == "Call get_handoff\n" for _, text, literal in tmux.sent_keys if literal) == 1
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables.get_variables(SESSION_ID)


@pytest.mark.asyncio
async def test_codex_readiness_keeps_prompt_until_compact_receipt(session_db: HubDatabase) -> None:
    attempt_id = "a" * 32
    mark_handoff_compact_continuation_pending(
        session_db, SESSION_ID, prompt="Call get_handoff", attempt_id=attempt_id
    )
    SessionVariableManager(session_db).set_variable(
        SESSION_ID,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {"attempt_id": attempt_id, "delivery_pending": True},
    )

    class ReadyTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            return "• Context compacted\n›"

    tmux = ReadyTmux()
    await _continue_after_codex_compaction_ready(
        session_db,
        pane=TmuxPaneIO(tmux, "%12"),
        pending_session_id=SESSION_ID,
        before_command="Before /compact\n›",
        poll_seconds=0,
        attempt_id=attempt_id,
    )

    assert tmux.sent_keys == []
    marker = SessionVariableManager(session_db).get_variables(SESSION_ID)[
        HANDOFF_COMPACT_CONTINUE_VARIABLE
    ]
    assert marker["attempt_id"] == attempt_id

    staged = stage_handoff_attempt(
        session_db,
        SESSION_ID,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Compacted", next_steps=["Continue"]),
        clear_session=False,
    )
    record_handoff_delivery(
        session_db,
        handoff_id=staged.handoff_record_id,
        attempt_id=attempt_id,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )
    with patch(
        "gobby.sessions.continuation_retry.await_before_agent",
        AsyncMock(return_value=True),
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command="Before /compact\n›",
            poll_seconds=0,
            attempt_id=attempt_id,
        )

    assert sum(text == "Call get_handoff\n" for _, text, literal in tmux.sent_keys if literal) == 1
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in SessionVariableManager(
        session_db
    ).get_variables(SESSION_ID)


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

    with (
        patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
        patch(
            "gobby.sessions.compact_continuation._composer_reader",
            return_value=_CODEX_READ,
        ),
        patch(
            "gobby.sessions.continuation_retry.await_before_agent",
            AsyncMock(return_value=True),
        ),
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [("%12", f"{prompt}\n", True), _ENTER]


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

    with (
        patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
        patch(
            "gobby.sessions.compact_continuation._composer_reader",
            return_value=_CODEX_READ,
        ),
        patch(
            "gobby.sessions.continuation_retry.await_before_agent",
            AsyncMock(return_value=True),
        ),
    ):
        await _continue_after_codex_compaction_ready(
            session_db,
            pane=TmuxPaneIO(tmux, "%12"),
            pending_session_id=SESSION_ID,
            before_command=before_command,
            poll_seconds=0,
        )

    assert tmux.sent_keys == [("%12", f"{prompt}\n", True), _ENTER]


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


def test_schedule_exception_restores_compact_prompt(session_db: HubDatabase) -> None:
    mark_handoff_compact_continuation_pending(
        session_db, SESSION_ID, prompt="Call get_handoff", attempt_id="current-attempt"
    )
    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        side_effect=RuntimeError("terminal unavailable"),
    ):
        scheduled = consume_and_schedule_handoff_compact_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(id=SESSION_ID),
        )

    assert scheduled is False
    marker = SessionVariableManager(session_db).get_variables(SESSION_ID)[
        HANDOFF_COMPACT_CONTINUE_VARIABLE
    ]
    assert marker["attempt_id"] == "current-attempt"


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
_CODEX_READ = IdleDetector(BundledDetectionRegistry(), "codex").composer_read
_PULL_PROMPT = "Continue the claimed task by calling get_handoff first."
_CLAUDE_DRAIN = [("%12", tmux_key_name(key), False) for key in composer_clear_sequence("claude")]


def _claude_frame(row: str) -> str:
    rule = "─" * 20
    return f"⏺ done\n{rule}\n{row}\n{rule}\n   Fable 5.1  12%\n"


def _wrapped_continuation_frame(draft: str, cli_source: str, width: int) -> str:
    rows = textwrap.wrap(draft, width=width - 2, break_long_words=False)
    marker = "❯" if cli_source == "claude" else "›"
    frame = marker + " " + (rows[0] if rows else "")
    frame += "".join("\n  " + row for row in rows[1:])
    if cli_source == "claude":
        return _claude_frame(frame)
    return frame + "\n\n  GPT-6-Sol xhigh · ~/Projects/gobby · 0.5.0\n  ? for shortcuts\n"


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
        # The composer cannot hold our prompt before this path types it, so the
        # pre-write gate probe reads empty; the held-prompt states below only
        # appear after the write.
        typed = any(
            text == f"{self.prompt}\n" for _pane, text, literal in self.sent_keys if literal
        )
        released = (
            self.releases_after_enters is not None and self.enters >= self.releases_after_enters
        )
        held = typed and not released
        return _claude_frame(f"❯ {self.prompt}" if held else "❯\xa0")


class _ForeignDraftTmux(_FakeTmux):
    """Composer that reads empty until our prompt is written, then shows other text.

    Models the operator's own draft (or a CLI rewrite) appearing after the write:
    the post-Enter verify must treat a draft that is not our prompt as proof that
    our prompt left the composer.
    """

    def __init__(self, composer_text: str) -> None:
        super().__init__()
        self._foreign = composer_text

    async def snapshot_lines(
        self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        if lines != COMPOSER_PROBE_LINES:
            return await super().snapshot_lines(pane_id, lines, mode=mode)
        self.composer_modes.append(mode)
        wrote = any(
            text == f"{_PULL_PROMPT}\n" for _pane, text, literal in self.sent_keys if literal
        )
        return self._foreign if wrote else _claude_frame("❯\xa0")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("state", ["draft", "unknown", "probe_error"])
async def test_pull_prompt_refuses_unconfirmed_composer_and_keeps_durable_message(
    session_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, cli_source: str, state: str
) -> None:
    """An unreadable frame may conceal a draft; refusal must write nothing."""
    monkeypatch.setattr("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0)
    tmux = _FakeTmux()
    tmux.composer_text = "unreadable frame containing the operator's unsent draft"

    def read(_snapshot: str | None) -> ComposerRead:
        if state == "probe_error":
            raise RuntimeError("snapshot parser failed")
        return ComposerRead("draft" if state == "draft" else "unknown", "operator draft")

    def retain() -> None:
        persist_pull_prompt_message(session_db, SESSION_ID, _PULL_PROMPT, "refused-attempt")

    sent = await _send_handoff_compact_continuation(
        TmuxPaneIO(tmux, "%12"),
        _PULL_PROMPT,
        SESSION_ID,
        delay_seconds=0,
        cli_source=cli_source,
        composer_read=read,
        on_send_failure=retain,
    )

    assert sent is False
    assert tmux.sent_keys == []
    assert tmux.composer_text == "unreadable frame containing the operator's unsent draft"
    queued = InterSessionMessageManager(session_db).get_undelivered_messages(SESSION_ID)
    assert [(message.content, message.message_type) for message in queued] == [
        (_PULL_PROMPT, "handoff_continuation")
    ]


class _RedrawingTmux(_FakeTmux):
    """A Claude pane still redrawing after compaction: its first probes show no frame."""

    def __init__(self, unframed_probes: int) -> None:
        super().__init__()
        self.unframed_probes = unframed_probes

    async def snapshot_lines(
        self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        if lines == COMPOSER_PROBE_LINES and self.unframed_probes > 0:
            self.unframed_probes -= 1
            self.composer_modes.append(mode)
            return "✻ Conversation compacted (ctrl+o for history)\n"
        return await super().snapshot_lines(pane_id, lines, mode=mode)


@pytest.mark.asyncio
async def test_pull_prompt_reprobes_a_composer_still_redrawing_after_compaction() -> None:
    """SessionStart(compact) can arrive before Claude redraws its composer frame.

    A single unframed probe used to refuse the pull prompt and queue it with no
    wake, stranding the seat until an unrelated message arrived (#23727).
    """
    tmux = _RedrawingTmux(unframed_probes=2)
    tmux.composer_text = _claude_frame("❯\xa0")
    assert _CLAUDE_READ("✻ Conversation compacted (ctrl+o for history)\n").state == "unknown"
    failures: list[int] = []

    assert await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is True

    assert failures == []
    assert tmux.unframed_probes == 0
    assert any(
        text.startswith(_PULL_PROMPT[:12]) for _pane, text, literal in tmux.sent_keys if literal
    )


@pytest.mark.asyncio
async def test_pull_prompt_composer_refusal_logs_session_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tmux = _FakeTmux()
    tmux.composer_text = _claude_frame("❯ half-typed operator note")
    assert _CLAUDE_READ(tmux.composer_text).state == "draft"
    failures: list[int] = []

    with caplog.at_level(logging.WARNING, logger="gobby.sessions.compact_continuation"):
        sent = await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0))

    assert sent is False
    assert failures == [0]
    assert tmux.sent_keys == []
    assert any(
        SESSION_ID in record.getMessage()
        and "composer holds an operator draft" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_pull_prompt_write_gate_refusal_logs_session_and_reason(
    session_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    SessionManager(session_db).update_session_status(SESSION_ID, "awaiting_input")
    tmux = _FakeTmux()
    tmux.composer_text = _claude_frame("❯\xa0")

    with caplog.at_level(logging.WARNING, logger="gobby.sessions.compact_continuation"):
        sent = await _send_handoff_compact_continuation(
            TmuxPaneIO(tmux, "%12"),
            _PULL_PROMPT,
            SESSION_ID,
            delay_seconds=0,
            cli_source="claude",
            composer_read=_CLAUDE_READ,
            db=session_db,
        )

    assert sent is False
    assert tmux.sent_keys == []
    assert any(
        SESSION_ID in record.getMessage() and "awaiting_input" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_pull_prompt_waits_for_the_shared_composer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rival composer writer blocks the pull prompt from interleaving."""
    from gobby.terminals import composer_lock as composer_lock_module
    from gobby.terminals.composer_lock import composer_action_lock

    class _Coordinator:
        def __init__(self) -> None:
            self.locks: dict[str, asyncio.Lock] = {}

        def logical_action_lock(self, terminal_id: str) -> asyncio.Lock:
            return self.locks.setdefault(terminal_id, asyncio.Lock())

    coordinator = _Coordinator()
    monkeypatch.setattr(composer_lock_module, "_coordinator", coordinator)

    tmux = _FakeTmux()
    tmux.composer_text = _claude_frame("❯\xa0")
    assert _CLAUDE_READ(tmux.composer_text).state == "empty"
    async with composer_action_lock("%12"):
        send = asyncio.create_task(_send_pull_prompt(tmux))
        for _ in range(5):
            await asyncio.sleep(0)
        # The rival holds the lock, so the prompt has typed nothing yet.
        assert tmux.sent_keys == []
        assert coordinator.locks["%12"].locked()
    assert await send is True
    assert any(
        text.startswith(_PULL_PROMPT[:12]) for _pane, text, literal in tmux.sent_keys if literal
    )


async def test_confirmed_empty_pull_prompt_types_without_draining() -> None:
    """A positive empty read leaves the drain nothing to remove but late keystrokes (#22915)."""
    tmux = _FakeTmux()
    tmux.composer_text = _claude_frame("❯\xa0")
    assert _CLAUDE_READ(tmux.composer_text).state == "empty"

    assert await _send_pull_prompt(tmux) is True

    assert [text for _pane, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
    assert [key for _pane, key, literal in tmux.sent_keys if not literal] == ["Enter"]


@pytest.mark.parametrize(
    "initial",
    ["empty", "held", "foreign", "prefix", "prefix-only", "changed", "before-write-changed"],
)
@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("width", [120, 200])
async def test_native_continuation_retries_only_its_exact_held_payload(
    monkeypatch: pytest.MonkeyPatch, initial: str, cli_source: str, width: int
) -> None:
    prompt = build_handoff_continue_prompt()
    composer = {
        "empty": "",
        "held": prompt,
        "foreign": "operator private draft",
        "prefix": prompt + " operator addition",
        "prefix-only": textwrap.wrap(prompt, width=width - 2)[0],
        "changed": "",
        "before-write-changed": "",
    }[initial]
    operations: list[tuple[str, str]] = []
    enter_attempts = 0
    snapshot_calls = 0
    runtime = NativeTerminalRuntime(cast(Any, object()))

    async def batch(targets: list[NativeBatchTarget]) -> list[NativeBatchResult]:
        nonlocal composer, enter_attempts
        operation = targets[0].operations[0]
        operations.append((operation.kind, operation.payload))
        if operation.kind == "text":
            composer = prompt
        else:
            assert operation.payload == "enter"
            enter_attempts += 1
            if enter_attempts == 1:
                if initial == "changed":
                    composer = prompt + " operator addition"
                    return [NativeBatchResult("pane-input", Delivered())]
                return [
                    NativeBatchResult(
                        "pane-input", NativeBatchFailure("none", "pty_busy", "queue full")
                    )
                ]
            composer = ""
        return [NativeBatchResult("pane-input", Delivered())]

    async def snapshot(lines: int = 12, *, mode: SnapshotMode = "text") -> str:
        nonlocal composer, snapshot_calls
        snapshot_calls += 1
        if initial == "before-write-changed" and snapshot_calls == 2:
            composer = "private operator note"
        return _wrapped_continuation_frame(composer, cli_source, width)

    monkeypatch.setattr(runtime, "write_batch", batch)
    pane = RuntimePaneIO(runtime, SimpleNamespace(id="continuation-terminal"))
    monkeypatch.setattr(pane, "snapshot", snapshot)
    monkeypatch.setattr("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0)
    failures: list[int] = []
    sent = await _send_handoff_compact_continuation(
        pane,
        prompt,
        SESSION_ID,
        delay_seconds=0,
        cli_source=cli_source,
        composer_read=_CLAUDE_READ if cli_source == "claude" else _CODEX_READ,
        on_send_failure=lambda: failures.append(0),
    )
    if initial == "before-write-changed":
        assert sent is False
        assert operations == []
        assert failures == [0]
        assert composer == "private operator note"
    elif initial == "changed":
        assert sent is False
        assert operations == [("text", prompt + "\n"), ("key", "enter")]
        assert failures == [0]
        assert composer == prompt + " operator addition"
    elif initial in {"foreign", "prefix", "prefix-only"}:
        assert sent is False
        assert operations == []
        assert failures == [0]
        expected = {
            "foreign": "operator private draft",
            "prefix": prompt + " operator addition",
            "prefix-only": textwrap.wrap(prompt, width=width - 2)[0],
        }[initial]
        assert composer == expected
    else:
        assert sent is True
        assert composer == ""
        assert failures == []
        assert operations == ([("text", prompt + "\n")] if initial == "empty" else []) + [
            ("key", "enter"),
            ("key", "enter"),
        ]


@pytest.mark.parametrize("draft", ["private operator note", _PULL_PROMPT + " operator addition"])
async def test_continuation_lifecycle_retry_refuses_every_other_draft(draft: str) -> None:
    tmux = _FakeTmux()
    tmux.composer_text = _claude_frame("❯ " + draft)
    assert _CLAUDE_READ(tmux.composer_text).state == "draft"
    sent = await continuation_retry.resubmit_continuation(
        TmuxPaneIO(tmux, "%12"),
        _PULL_PROMPT,
        SESSION_ID,
        cli_source="claude",
        composer_read=_CLAUDE_READ,
        verify_seconds=0,
    )
    assert sent is False
    assert tmux.sent_keys == []
    assert tmux.composer_text == _claude_frame("❯ " + draft)


@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("draft_kind", ["held", "suffix", "prefix-only"])
async def test_lifecycle_retry_matches_whole_wrapped_continuation(
    cli_source: str, draft_kind: str
) -> None:
    prompt = build_handoff_continue_prompt()
    draft = {
        "held": prompt,
        "suffix": prompt + " operator addition",
        "prefix-only": textwrap.wrap(prompt, width=118)[0],
    }[draft_kind]
    tmux = _FakeTmux()
    tmux.composer_text = _wrapped_continuation_frame(draft, cli_source, 120)
    sent = await continuation_retry.resubmit_continuation(
        TmuxPaneIO(tmux, "%12"),
        prompt,
        SESSION_ID,
        cli_source=cli_source,
        composer_read=_CLAUDE_READ if cli_source == "claude" else _CODEX_READ,
        verify_seconds=0,
    )
    assert sent is (draft_kind == "held")
    assert tmux.sent_keys == ([("%12", "Enter", False)] if draft_kind == "held" else [])
    assert tmux.composer_text == _wrapped_continuation_frame(draft, cli_source, 120)


@pytest.mark.parametrize("cli_source", ["claude", "codex"])
async def test_lifecycle_repaste_preserves_draft_that_appears_before_write(cli_source: str) -> None:
    class DraftBeforeWriteTmux(_FakeTmux):
        probes = 0

        async def snapshot_lines(
            self, pane_id: str, lines: int = 5, *, mode: SnapshotMode = "text"
        ) -> str:
            self.probes += 1
            return _wrapped_continuation_frame(
                "private operator note" if self.probes >= 3 else "", cli_source, 120
            )

    tmux = DraftBeforeWriteTmux()
    sent = await continuation_retry.resubmit_continuation(
        TmuxPaneIO(tmux, "%12"),
        build_handoff_continue_prompt(),
        SESSION_ID,
        cli_source=cli_source,
        composer_read=_CLAUDE_READ if cli_source == "claude" else _CODEX_READ,
        verify_seconds=0,
    )
    assert sent is False
    assert tmux.sent_keys == [("%12", "Enter", False)]


class TestPullPromptFallback:
    """The pull prompt survives a failed send and never submits an operator draft."""

    @pytest.mark.asyncio
    async def test_a_clean_submit_is_one_write_and_one_enter(self) -> None:
        tmux = _StickyComposerTmux(releases_after_enters=0)

        assert await _send_pull_prompt(tmux) is True
        assert tmux.enters == 1
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]
        # Every composer probe -- the pre-write gate and the post-Enter verify --
        # must use styling; faint suggestions are only distinguishable in ANSI.
        assert set(tmux.composer_modes) == {"ansi"}

    @pytest.mark.asyncio
    async def test_a_paste_that_kept_its_newline_is_submitted_by_the_enter(self) -> None:
        tmux = _StickyComposerTmux(releases_after_enters=1)

        assert await _send_pull_prompt(tmux) is True
        assert tmux.enters == 1
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]

    @pytest.mark.asyncio
    async def test_a_different_operator_draft_falls_back_without_another_enter(self) -> None:
        """A different draft proves neither submission nor permission to retry Enter."""
        tmux = _ForeignDraftTmux(_claude_frame("❯ the operator typed this"))
        failures: list[int] = []

        assert await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is False
        assert failures == [0]
        assert [text for _p, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
        assert sum(1 for _p, key, literal in tmux.sent_keys if key == "Enter" and not literal) == 1

    @pytest.mark.asyncio
    async def test_a_faint_suggestion_is_empty_and_proves_submission(self) -> None:
        """An unaccepted suggestion is not an operator draft."""
        tmux = _ForeignDraftTmux(
            _claude_frame("\x1b[39m❯\xa0\x1b[2mrun\x1b[0m \x1b[2mthe tests\x1b[0m")
        )

        assert await _send_pull_prompt(tmux) is True
        assert [text for _p, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
        assert sum(1 for _p, key, literal in tmux.sent_keys if key == "Enter" and not literal) == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_composer_after_the_enter_falls_back_without_retyping(
        self,
    ) -> None:
        """An unread composer after the Enter is unverified, never delivered (#23188).

        The prompt is typed once and entered once. The durable fallback then queues
        the pull prompt as next-turn context, which never types into the composer,
        so an unverified delivery cannot become a second submission.
        """

        class UnreadableAfterEnterTmux(_FakeTmux):
            composer_text: str | None = _claude_frame("❯\xa0")

            async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
                delivered = await super().send_keys(pane_id, text, literal=literal)
                if text == "Enter" and not literal:
                    self.composer_text = None
                return delivered

        tmux = UnreadableAfterEnterTmux()
        assert _CLAUDE_READ(tmux.composer_text).state == "empty"
        failures: list[int] = []

        assert await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is False
        assert failures == [0]
        assert [text for _p, text, literal in tmux.sent_keys if literal] == [f"{_PULL_PROMPT}\n"]
        assert sum(1 for _p, key, literal in tmux.sent_keys if key == "Enter" and not literal) == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_lifecycle_is_not_composer_only_success(
        self, session_db: HubDatabase
    ) -> None:
        """An unreadable turn lifecycle cannot confirm BEFORE_AGENT.

        With a hub database the continuation is confirmed by the session's own
        BEFORE_AGENT, not by the composer. When the lifecycle read fails the
        screen-verified type must not be reported as delivered, because it can
        neither confirm nor refute the hook; the durable fallback takes over
        (#22706 MEDIUM).
        """
        tmux = _StickyComposerTmux(releases_after_enters=0)
        failures: list[int] = []

        with (
            patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
            patch("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0),
            patch(
                "gobby.sessions.compact_continuation.turn_lifecycle_generation",
                side_effect=RuntimeError("attention state unavailable"),
            ),
        ):
            sent = await _send_handoff_compact_continuation(
                TmuxPaneIO(tmux, "%12"),
                _PULL_PROMPT,
                SESSION_ID,
                delay_seconds=0,
                cli_source="claude",
                on_send_failure=lambda: failures.append(0),
                composer_read=_CLAUDE_READ,
                db=session_db,
            )

        assert sent is False
        assert failures == [0]

    @pytest.mark.asyncio
    async def test_a_readable_lifecycle_still_confirms_by_before_agent(
        self, session_db: HubDatabase
    ) -> None:
        """The readable path keeps confirming by BEFORE_AGENT, not the composer."""
        from gobby.sessions import continuation_retry

        tmux = _StickyComposerTmux(releases_after_enters=0)
        checks: list[int | None] = []

        async def fake_await_before_agent(
            _db: HubDatabase,
            _session_id: str,
            *,
            baseline_generation: int | None,
            **_kwargs: Any,
        ) -> bool:
            checks.append(baseline_generation)
            return True

        with (
            patch("gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS", 0.0),
            patch("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0),
            patch.object(continuation_retry, "await_before_agent", fake_await_before_agent),
        ):
            sent = await _send_handoff_compact_continuation(
                TmuxPaneIO(tmux, "%12"),
                _PULL_PROMPT,
                SESSION_ID,
                delay_seconds=0,
                cli_source="claude",
                composer_read=_CLAUDE_READ,
                db=session_db,
            )

        assert sent is True
        # The readable lifecycle produced a real baseline for the hook check.
        assert checks == [0]

    @pytest.mark.asyncio
    async def test_a_prompt_that_never_leaves_is_drained_then_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tmux = _StickyComposerTmux()
        failures: list[int] = []

        with caplog.at_level(logging.ERROR, logger="gobby.sessions.compact_continuation"):
            assert (
                await _send_pull_prompt(tmux, on_send_failure=lambda: failures.append(0)) is False
            )
        assert failures == [0]
        records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "handoff_continuation_not_submitted"
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        # The held draft gets one bare-Enter retry, without a retype.
        assert tmux.typed == [f"{_PULL_PROMPT}\n"]
        assert tmux.enters == 2
        # The draft is ours, so it is drained before the durable fallback delivers it.
        assert tmux.sent_keys[-len(_CLAUDE_DRAIN) :] == _CLAUDE_DRAIN

    @pytest.mark.asyncio
    async def test_unsubmitted_prompt_that_cannot_be_cleared_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tmux = _StickyComposerTmux()
        with (
            patch(
                "gobby.sessions.compact_continuation.clear_composer",
                side_effect=[(False, "drain failed")],
            ),
            caplog.at_level(logging.ERROR, logger="gobby.sessions.compact_continuation"),
        ):
            assert await _send_pull_prompt(tmux) is False

        assert any(
            record.levelno == logging.ERROR
            and "Composer still holds the unsubmitted continuation prompt" in record.getMessage()
            for record in caplog.records
        )

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
                "gobby.sessions.compact_continuation._continuation_pane",
                return_value=TmuxPaneIO(tmux, "%12"),
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
                "gobby.sessions.compact_continuation._continuation_pane",
                return_value=TmuxPaneIO(_FakeTmux(), "%12"),
            ),
            patch(
                "gobby.sessions.compact_continuation._type_handoff_compact_continuation",
                new=AsyncMock(return_value=SubmitResult(False)),
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

    # The native runtime draws no composer, so the submit is unverified: the prompt
    # is written once and Enter sent once, then the composer is drained, never retyped.
    assert writes.index(("text", prompt, True)) < writes.index(("key", "enter"))
    assert writes.count(("text", prompt, True)) == 1
    assert writes.count(("key", "enter")) == 1
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


def test_continuation_pane_rejects_legacy_tmux_context() -> None:
    session = SimpleNamespace(id="session-1", terminal_context={"tmux_pane": "%12"})
    manager = MagicMock()
    manager.get_live_for_session.return_value = None
    registry = MagicMock()

    pane = _continuation_pane(session, "session-1", manager, registry)

    assert pane is None
    registry.resolve.assert_not_called()
