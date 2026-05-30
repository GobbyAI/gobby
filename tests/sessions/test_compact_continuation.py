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
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


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
        id="sess-1",
        terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
    )
    tmux = _FakeTmux()

    assert mark_compact_self_continuation_pending(db, "sess-1")
    with patch(
        "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
        return_value=tmux,
    ):
        scheduled = schedule_compact_self_continuation_fallback(
            db,
            pending_session_id="sess-1",
            target_session=session,
            delay_seconds=0,
        )
        assert scheduled is True
        await drain_asyncio_tasks()

    assert tmux.sent_keys == [("%12", f"{COMPACT_SELF_CONTINUE_PROMPT}\n", True)]
    variables = SessionVariableManager(db).get_variables("sess-1")
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables


def test_persist_compact_resume_required_skills_merges_required_and_loaded_skills(
    hub_db: HubDatabase,
) -> None:
    db = hub_db
    sv_mgr = SessionVariableManager(db)
    sv_mgr.merge_variables(
        "sess-1",
        {
            "required_skills": ["python"],
            "claimed_task_required_skills": ["python", "development-discipline"],
            "loaded_skills": ["code-index"],
        },
    )

    skills = persist_compact_resume_required_skills(db, "sess-1")

    assert skills == ["python", "code-index", "development-discipline"]
    variables = sv_mgr.get_variables("sess-1")
    assert variables[COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE] == skills


def test_build_compact_self_continue_prompt_includes_skill_fetch_directives() -> None:
    prompt = build_compact_self_continue_prompt(["python", "python", "development-discipline"])

    assert "progressive discovery" in prompt
    assert prompt.count(skill_fetch_directive("python")) == 1
    assert skill_fetch_directive("development-discipline") in prompt


@pytest.mark.asyncio
async def test_fallback_noops_when_marker_was_already_consumed(
    hub_db: HubDatabase,
) -> None:
    """Skip prompt delivery when the pending marker is already absent."""
    db = hub_db
    session = SimpleNamespace(
        id="sess-1",
        terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
    )
    tmux = _FakeTmux()

    with patch(
        "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
        return_value=tmux,
    ):
        scheduled = schedule_compact_self_continuation_fallback(
            db,
            pending_session_id="sess-1",
            target_session=session,
            delay_seconds=0,
        )
        assert scheduled is True
        await drain_asyncio_tasks()

    assert tmux.sent_keys == []
    variables = SessionVariableManager(db).get_variables("sess-1")
    assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
