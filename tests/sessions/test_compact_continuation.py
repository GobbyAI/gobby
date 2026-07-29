"""Tests for compact_self continuation marker delivery."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.sessions.compact_continuation import (
    _COMPACT_SELF_CONTINUATION_TASKS,
    COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE,
    COMPACT_SELF_CONTINUE_VARIABLE,
    _merge_session_variable,
    _pop_session_variable,
    build_compact_self_continue_prompt,
    consume_and_schedule_compact_self_continuation,
    consume_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
    persist_compact_resume_required_skills,
    schedule_compact_self_continuation,
)
from gobby.skills.formatting import skill_fetch_batch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_SESSION_ID = "00000000-0000-4000-8000-000000000002"
PROJECT_ID = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def session_db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "compact-continuation-test"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (SESSION_ID, "compact-session", "test-machine", "codex", PROJECT_ID),
    )
    return hub_db


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

    with patch(
        "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
        return_value=tmux,
    ):
        assert schedule_compact_self_continuation(session, prompt, delay_seconds=0)
        await send_started.wait()

        assert len(_COMPACT_SELF_CONTINUATION_TASKS) == 1
        assert tmux.sent_keys == [("%12", f"{prompt}\n", True)]
        task = next(iter(_COMPACT_SELF_CONTINUATION_TASKS))

        release_send.set()
        await task
        await drain_asyncio_tasks()

    assert not _COMPACT_SELF_CONTINUATION_TASKS


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


def test_persist_compact_resume_required_skills_reloads_claimed_task_skill(
    session_db: HubDatabase,
) -> None:
    db = session_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        SESSION_ID,
        {
            "required_skills": ["python"],
            "claimed_task_required_skills": ["tasks", "python", "development-discipline"],
            "loaded_skills": ["code-index", "tasks"],
        },
    )

    skills = persist_compact_resume_required_skills(db, SESSION_ID)

    assert skills == ["loading-skills", "python", "tasks", "development-discipline"]
    variables = sv_mgr.get_variables(SESSION_ID)
    assert variables[COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE] == skills


def test_build_compact_self_continue_prompt_includes_skill_fetch_directives() -> None:
    prompt = build_compact_self_continue_prompt(
        ["python", "python", "development-discipline"],
        summary_session_id=SOURCE_SESSION_ID,
    )

    assert prompt.startswith("Continue where you last left off.")
    assert "rejected or cancelled compact_self tool-use message" in prompt
    assert "expected terminal self-compaction delivery, not user refusal" in prompt
    assert "`<!-- gobby:injected-context:begin -->`" in prompt
    assert prompt.index("use that injected context directly") < prompt.index(
        "gobby-sessions.wait_for_summary"
    )
    assert f'gobby-sessions.wait_for_summary(session_id="{SOURCE_SESSION_ID}")' in prompt
    assert "`completed=false`" in prompt
    assert "directly" in prompt
    assert "list_mcp_servers" not in prompt
    assert "list_tools" not in prompt
    assert "get_tool_schema" not in prompt
    assert (
        skill_fetch_batch_directive(["loading-skills", "python", "development-discipline"])
        in prompt
    )
