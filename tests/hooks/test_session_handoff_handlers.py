"""Focused compact-continuation regressions retained at the contract path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.event_handlers._session_start.in_place_compact import (
    apply_in_place_compact_context_loss,
)
from gobby.sessions.compact_continuation import (
    COMPACT_RESUME_LEASED_TOOLS_VARIABLE,
    persist_handoff_resume_leased_tools,
)
from gobby.storage import workspace_machine_scope
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


class TestPrepareCompactContinuationVariables:
    def test_in_place_compact_preserves_snapshotted_schema_leases(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = "d60ca585-39ea-4280-8b2f-9bd641bd6012"
        LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
        monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
        session_id = session_manager.register_session(
            external_id="grok-schema-lease-restore",
            machine_id=machine_id,
            source="grok",
            project_id=sample_project["id"],
        )
        variables = SessionVariableManager(session_manager.db)
        variables.set_variable(
            session_id,
            "unlocked_tools",
            ["gobby-tasks:create_task", "gobby-memory:search_memories"],
        )
        expected = persist_handoff_resume_leased_tools(session_manager.db, session_id)
        handler = SimpleNamespace(_session_manager=session_manager, _task_manager=None)

        apply_in_place_compact_context_loss(handler, session_id)

        stored = variables.get_variables(session_id)
        assert stored["unlocked_tools"] == []
        assert stored[COMPACT_RESUME_LEASED_TOOLS_VARIABLE] == expected
