"""Tests for compact_self continuation marker delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.sessions.compact_continuation import (
    COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE,
    COMPACT_SELF_CONTINUE_PROMPT,
    COMPACT_SELF_CONTINUE_VARIABLE,
    build_compact_self_continue_prompt,
    mark_compact_self_continuation_pending,
    persist_compact_resume_required_skills,
    schedule_compact_self_continuation_fallback,
)
from gobby.skills.formatting import skill_fetch_batch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_SESSION_ID = "00000000-0000-4000-8000-000000000002"


class _FakeTmux:
    def __init__(self) -> None:
        self.sent_keys: list[tuple[str, str, bool]] = []

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
        return True


@pytest.mark.asyncio
async def test_fallback_consumes_pending_marker_and_sends_prompt(
    hub_db: HubDatabase,
) -> None:
    """Send one continuation prompt and clear the pending marker."""
    db = hub_db
    session = SimpleNamespace(
        id=SESSION_ID,
        terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
    )
    tmux = _FakeTmux()

    assert mark_compact_self_continuation_pending(db, SESSION_ID)
    with patch(
        "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
        return_value=tmux,
    ):
        scheduled = schedule_compact_self_continuation_fallback(
            db,
            pending_session_id=SESSION_ID,
            target_session=session,
            delay_seconds=0,
        )
        assert scheduled is True
        await drain_asyncio_tasks()

    assert tmux.sent_keys == [("%12", f"{COMPACT_SELF_CONTINUE_PROMPT}\n", True)]
    variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables


def test_pending_marker_stores_summary_session_id(hub_db: HubDatabase) -> None:
    assert mark_compact_self_continuation_pending(
        hub_db,
        SESSION_ID,
        summary_session_id=SOURCE_SESSION_ID,
    )

    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[COMPACT_SELF_CONTINUE_VARIABLE]["summary_session_id"] == SOURCE_SESSION_ID


def test_persist_compact_resume_required_skills_excludes_loaded_skills(
    hub_db: HubDatabase,
) -> None:
    db = hub_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        SESSION_ID,
        {
            "required_skills": ["python"],
            "claimed_task_required_skills": ["python", "development-discipline"],
            "loaded_skills": ["code-index", "task-transitions"],
        },
    )

    skills = persist_compact_resume_required_skills(db, SESSION_ID)

    assert skills == ["loading-skills", "python", "development-discipline"]
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
    assert "progressive discovery" in prompt
    assert prompt.count("list_mcp_servers") == 1
    assert (
        skill_fetch_batch_directive(["loading-skills", "python", "development-discipline"])
        in prompt
    )


@pytest.mark.asyncio
async def test_fallback_noops_when_marker_was_already_consumed(
    hub_db: HubDatabase,
) -> None:
    """Skip prompt delivery when the pending marker is already absent."""
    db = hub_db
    session = SimpleNamespace(
        id=SESSION_ID,
        terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
    )
    tmux = _FakeTmux()

    with patch(
        "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
        return_value=tmux,
    ):
        scheduled = schedule_compact_self_continuation_fallback(
            db,
            pending_session_id=SESSION_ID,
            target_session=session,
            delay_seconds=0,
        )
        assert scheduled is True
        await drain_asyncio_tasks()

    assert tmux.sent_keys == []
    variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
