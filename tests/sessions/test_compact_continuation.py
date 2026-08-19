"""Tests for compact_self continuation marker delivery."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from gobby.sessions.compact_continuation import (
    _COMPACT_SELF_CONTINUATION_TASKS,
    COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE,
    COMPACT_RESUME_EXCLUDED_SKILLS,
    COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE,
    COMPACT_SELF_CONTINUE_VARIABLE,
    COMPACT_SELF_INTERRUPT_WARNING,
    WORKFLOW_REQUESTED_SKILLS_VARIABLE,
    CodexRolloutCursor,
    CodexRolloutObservationError,
    _continue_after_codex_compaction_ready,
    _merge_session_variable,
    _pop_session_variable,
    build_compact_self_continue_prompt,
    clear_compact_self_continuation_pending,
    consume_and_schedule_compact_self_continuation,
    consume_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
    persist_compact_resume_required_skills,
    schedule_codex_compact_self_continuation_readiness,
    schedule_compact_self_continuation,
)
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

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

    with pytest.raises(CodexRolloutObservationError, match="replaced"):
        cursor.saw_fresh_turn_aborted()


def test_codex_rollout_cursor_rejects_truncated_transcript(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"session_meta"}\n')
    cursor = CodexRolloutCursor.at_eof(rollout)
    rollout.write_bytes(b"")

    with pytest.raises(CodexRolloutObservationError, match="truncated"):
        cursor.saw_fresh_turn_aborted()


class _FakeTmux:
    def __init__(self) -> None:
        self.sent_keys: list[tuple[str, str, bool]] = []

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
        return True


@pytest.mark.asyncio
async def test_scheduled_task_is_retained_and_multiline_prompt_is_sent_once() -> None:
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class BlockingTmux(_FakeTmux):
        async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
            self.sent_keys.append((pane_id, text, literal))
            send_started.set()
            await release_send.wait()
            return True

    session = SimpleNamespace(id=SESSION_ID, terminal_context={"tmux_pane": "%12"})
    tmux = BlockingTmux()
    prompt = "Continue the task.\nPreserve the existing context."

    with (
        patch(
            "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
            return_value=tmux,
        ),
        patch(
            "gobby.sessions.compact_continuation.COMPACT_SELF_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
            0.0,
        ),
    ):
        assert schedule_compact_self_continuation(session, prompt, delay_seconds=0)
        await send_started.wait()

        assert len(_COMPACT_SELF_CONTINUATION_TASKS) == 1
        assert tmux.sent_keys == [("%12", f"{prompt}\n", True)]
        task = next(iter(_COMPACT_SELF_CONTINUATION_TASKS))

        release_send.set()
        await task
        await drain_asyncio_tasks()

    assert tmux.sent_keys[-1] == ("%12", "Enter", False)
    assert not _COMPACT_SELF_CONTINUATION_TASKS


@pytest.mark.asyncio
async def test_codex_waits_for_fresh_compaction_marker_before_continuing(
    session_db: HubDatabase,
) -> None:
    prompt = "Continue the claimed task."
    mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        prompt=prompt,
        attempt_id="current-attempt",
    )
    before_command = "Earlier output\n• Context compacted\n›"

    class ReadinessTmux(_FakeTmux):
        def __init__(self) -> None:
            super().__init__()
            self.outputs = iter(
                [
                    before_command,
                    f"{before_command}\nCompacting conversation",
                    f"{before_command}\n• Context compacted\n›",
                ]
            )

        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            assert pane_id == "%12"
            assert lines == 100
            return next(self.outputs)

    tmux = ReadinessTmux()

    with patch(
        "gobby.sessions.compact_continuation.COMPACT_SELF_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
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
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]
    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables


@pytest.mark.asyncio
async def test_codex_readiness_stops_when_attempt_marker_is_replaced(
    session_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mark_compact_self_continuation_pending(
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
    marker = variables[COMPACT_SELF_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert "Timed out waiting for Codex compact readiness" not in caplog.text


@pytest.mark.asyncio
async def test_codex_readiness_does_not_take_marker_replaced_during_capture(
    session_db: HubDatabase,
) -> None:
    mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="older-attempt",
    )

    class RacingTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            mark_compact_self_continuation_pending(
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
    marker = variables[COMPACT_SELF_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert tmux.sent_keys == []


@pytest.mark.asyncio
async def test_codex_readiness_stops_when_tmux_pane_disappears(
    session_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mark_compact_self_continuation_pending(
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
    marker = variables[COMPACT_SELF_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "current-attempt"
    assert "Timed out waiting for Codex compact readiness" not in caplog.text


@pytest.mark.asyncio
async def test_codex_send_failure_restores_pending_marker(session_db: HubDatabase) -> None:
    prompt = "Continue the claimed task."
    mark_compact_self_continuation_pending(session_db, SESSION_ID, prompt=prompt)

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
    assert variables[COMPACT_SELF_CONTINUE_VARIABLE]["prompt"] == prompt


@pytest.mark.asyncio
async def test_codex_detects_fresh_marker_when_old_marker_scrolls_out(
    session_db: HubDatabase,
) -> None:
    prompt = "Continue the claimed task."
    mark_compact_self_continuation_pending(session_db, SESSION_ID, prompt=prompt)
    before_command = "old\n• Context compacted\nshared one\nshared two"

    class RollingTmux(_FakeTmux):
        async def capture_pane(self, pane_id: str, *, lines: int) -> str:
            assert pane_id == "%12"
            assert lines == 100
            return "shared one\nshared two\n• Context compacted\n›"

    tmux = RollingTmux()

    with patch(
        "gobby.sessions.compact_continuation.COMPACT_SELF_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
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
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]


@pytest.mark.asyncio
async def test_codex_ignores_compaction_marker_text_in_prose(
    session_db: HubDatabase,
) -> None:
    prompt = "Continue the claimed task."
    mark_compact_self_continuation_pending(session_db, SESSION_ID, prompt=prompt)
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
        "gobby.sessions.compact_continuation.COMPACT_SELF_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
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
        ("%12", f"{prompt}\n", True),
        ("%12", "Enter", False),
    ]


def test_codex_readiness_rejects_missing_baseline(session_db: HubDatabase) -> None:
    session = SimpleNamespace(terminal_context={"tmux_pane": "%12"})

    assert not schedule_codex_compact_self_continuation_readiness(
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


def test_pending_marker_stores_summary_session_id(session_db: HubDatabase) -> None:
    assert mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        summary_session_id=SOURCE_SESSION_ID,
    )

    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert variables[COMPACT_SELF_CONTINUE_VARIABLE]["summary_session_id"] == SOURCE_SESSION_ID


def test_clear_pending_marker_removes_only_matching_compact_attempt(
    session_db: HubDatabase,
) -> None:
    sv_mgr = SessionVariableManager(session_db)
    assert mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="first-attempt",
    )
    assert mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="newer-attempt",
    )

    assert not clear_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="first-attempt",
    )
    marker = sv_mgr.get_variables(SESSION_ID)[COMPACT_SELF_CONTINUE_VARIABLE]
    assert marker["attempt_id"] == "newer-attempt"
    assert clear_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        attempt_id="newer-attempt",
    )
    assert COMPACT_SELF_CONTINUE_VARIABLE not in sv_mgr.get_variables(SESSION_ID)


def test_pending_marker_expires_from_its_creation_time(session_db: HubDatabase) -> None:
    created_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert mark_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        now=created_at,
    )

    prompt = consume_compact_self_continuation_pending(
        session_db,
        SESSION_ID,
        now=datetime(2026, 7, 28, 12, 0, 2, tzinfo=UTC),
        fresh_seconds=1,
    )

    assert prompt is None
    variables = SessionVariableManager(session_db).get_variables(SESSION_ID)
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables


def test_failed_schedule_restores_exact_pending_marker(session_db: HubDatabase) -> None:
    created_at = datetime.now(UTC).isoformat()
    sv_mgr = SessionVariableManager(session_db)
    payload = {
        "prompt": "continue exactly",
        "created_at": created_at,
        "summary_session_id": SOURCE_SESSION_ID,
    }
    sv_mgr.merge_variables(SESSION_ID, {COMPACT_SELF_CONTINUE_VARIABLE: payload})

    with patch(
        "gobby.sessions.compact_continuation.schedule_compact_self_continuation",
        return_value=False,
    ):
        scheduled = consume_and_schedule_compact_self_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(id=SESSION_ID),
        )

    assert scheduled is False
    assert sv_mgr.get_variables(SESSION_ID)[COMPACT_SELF_CONTINUE_VARIABLE] == payload


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
    sv_mgr.merge_variables(SESSION_ID, {COMPACT_SELF_CONTINUE_VARIABLE: old_payload})

    def fail_after_new_marker(*_args: object, **_kwargs: object) -> bool:
        sv_mgr.merge_variables(SESSION_ID, {COMPACT_SELF_CONTINUE_VARIABLE: new_payload})
        return False

    with patch(
        "gobby.sessions.compact_continuation.schedule_compact_self_continuation",
        side_effect=fail_after_new_marker,
    ):
        scheduled = consume_and_schedule_compact_self_continuation(
            session_db,
            pending_session_id=SESSION_ID,
            target_session=SimpleNamespace(id=SESSION_ID),
        )

    assert scheduled is False
    assert sv_mgr.get_variables(SESSION_ID)[COMPACT_SELF_CONTINUE_VARIABLE] == new_payload


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
    assert mark_compact_self_continuation_pending(session_db, marked_id)

    with patch(
        "gobby.sessions.compact_continuation.schedule_compact_self_continuation",
        return_value=True,
    ) as mock_schedule:
        scheduled = consume_and_schedule_compact_self_continuation(
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
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables


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
    assert mark_compact_self_continuation_pending(session_db, marked_id)

    scheduled = consume_and_schedule_compact_self_continuation(
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
    assert COMPACT_SELF_CONTINUE_VARIABLE in variables


def test_persist_compact_resume_required_skills_reloads_claimed_task_skill(
    session_db: HubDatabase,
) -> None:
    db = session_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        SESSION_ID,
        {
            "required_skills": ["loading-skills", "python"],
            "claimed_task_required_skills": ["tasks", "python", "development-discipline"],
            "loaded_skills": ["code-index", "tasks"],
        },
    )

    skill_tiers = persist_compact_resume_required_skills(db, SESSION_ID)

    assert skill_tiers == {
        "required": [
            "python",
            "tasks",
            "development-discipline",
            "code-index",
        ],
        "advisory": [],
    }
    variables = sv_mgr.get_variables(SESSION_ID)
    assert variables[COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE] == skill_tiers["required"]
    assert variables[COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE] == skill_tiers["advisory"]


def test_loaded_skills_remain_required_across_two_compactions(
    session_db: HubDatabase,
) -> None:
    db = session_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        SESSION_ID,
        {
            "required_skills": ["loading-skills", "python"],
            "additional_skills": ["pytest"],
            WORKFLOW_REQUESTED_SKILLS_VARIABLE: ["plan", "elicit"],
            "loaded_skills": ["code-index", "brevity", "code-index"],
        },
    )

    first_tiers = persist_compact_resume_required_skills(db, SESSION_ID)

    assert first_tiers == {
        "required": [
            "python",
            "plan",
            "elicit",
            "code-index",
        ],
        "advisory": ["pytest"],
    }

    sv_mgr.set_variable(SESSION_ID, "loaded_skills", [])
    assert sv_mgr.get_variables(SESSION_ID)["loaded_skills"] == []

    # Successful get_skill calls repopulate the current-context ledger in first-load order.
    sv_mgr.append_to_set_variable(
        SESSION_ID,
        "loaded_skills",
        ["code-index"],
        preserve_order=True,
    )
    sv_mgr.append_to_set_variable(
        SESSION_ID,
        "loaded_skills",
        ["brevity", "code-index"],
        preserve_order=True,
    )
    assert sv_mgr.get_variables(SESSION_ID)["loaded_skills"] == ["code-index", "brevity"]

    second_tiers = persist_compact_resume_required_skills(db, SESSION_ID)

    assert second_tiers == first_tiers


def test_meta_skills_never_enter_resume_tiers(session_db: HubDatabase) -> None:
    """brevity and loading-skills ride per-turn reminders, never reload tiers."""
    db = session_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        SESSION_ID,
        {
            "required_skills": ["loading-skills", "brevity"],
            "claimed_task_required_skills": ["brevity", "tasks"],
            WORKFLOW_REQUESTED_SKILLS_VARIABLE: ["loading-skills"],
            "loaded_skills": ["brevity", "loading-skills", "code-index"],
            "additional_skills": ["brevity", "restraint"],
        },
    )

    skill_tiers = persist_compact_resume_required_skills(db, SESSION_ID)

    assert skill_tiers == {
        "required": ["tasks", "code-index"],
        "advisory": ["restraint"],
    }
    assert not set(skill_tiers["required"]) & COMPACT_RESUME_EXCLUDED_SKILLS
    assert not set(skill_tiers["advisory"]) & COMPACT_RESUME_EXCLUDED_SKILLS


def test_reload_directive_normalized() -> None:
    """The typed trigger is one paste line; skill tiers ride the injected context."""
    prompt = build_compact_self_continue_prompt(summary_session_id=SOURCE_SESSION_ID)

    assert prompt.startswith("Continue where you last left off.")
    assert COMPACT_SELF_INTERRUPT_WARNING in prompt
    assert "`<!-- gobby:injected-context:begin -->`" in prompt
    assert prompt.index("use that injected context directly") < prompt.index(
        "gobby-sessions.wait_for_summary"
    )
    assert f'gobby-sessions.wait_for_summary(session_id="{SOURCE_SESSION_ID}")' in prompt
    assert "`completed=false`" in prompt
    assert "\n" not in prompt
    assert "Required tier" not in prompt
    assert "Advisory tier" not in prompt
    assert "get_skill" not in prompt


@pytest.mark.asyncio
async def test_load_skill_effect_flows_to_persisted_resume_prompt(
    session_db: HubDatabase,
) -> None:
    variables: dict[str, object] = {
        "required_skills": ["loading-skills", "python"],
        "additional_skills": ["pytest", "python", "hypothesis"],
        "loaded_skills": ["plan", "brevity", "pytest"],
    }
    context_parts: list[str] = []
    effects = EffectsMixin()

    block_reason = await effects._apply_effect(
        RuleEffect(type="load_skill", skill="plan"),
        cast(RuleDefinitionRow, SimpleNamespace()),
        variables,
        {},
        {},
        context_parts,
        [],
    )

    assert block_reason is None
    assert variables[WORKFLOW_REQUESTED_SKILLS_VARIABLE] == ["plan"]
    assert len(context_parts) == 1
    assert '"get_skill"' in context_parts[0]

    sv_mgr = SessionVariableManager(session_db)
    sv_mgr.merge_variables(SESSION_ID, variables)
    skill_tiers = persist_compact_resume_required_skills(session_db, SESSION_ID)
    prompt = build_compact_self_continue_prompt()

    assert skill_tiers == {
        "required": ["python", "plan", "pytest"],
        "advisory": ["hypothesis"],
    }
    # The inject-compact-handoff rule reads both persisted tiers into the
    # SessionStart injected context; the typed trigger stays skill-free.
    persisted = sv_mgr.get_variables(SESSION_ID)
    assert persisted[COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE] == skill_tiers["required"]
    assert persisted[COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE] == skill_tiers["advisory"]
    assert "get_skill" not in prompt
    assert "\n" not in prompt
