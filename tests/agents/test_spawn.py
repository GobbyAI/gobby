"""
Tests for terminal spawn prepare functions.

Verifies that prepare_terminal_spawn and prepare_codex_spawn_with_preflight
persist agent_run_id via update_terminal_pickup_metadata.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.spawn import (
    PreparedSpawn,
    prepare_codex_spawn_with_preflight,
    prepare_terminal_spawn,
)

pytestmark = pytest.mark.unit


def _make_session_manager(
    child_session_id: str = "child-sess-1", agent_depth: int = 1
) -> MagicMock:
    """Create a mock ChildSessionManager."""
    mock = MagicMock()
    child_session = MagicMock()
    child_session.id = child_session_id
    child_session.agent_depth = agent_depth
    mock.create_child_session.return_value = child_session
    mock.update_terminal_pickup_metadata.return_value = child_session
    return mock


class TestPrepareTerminalSpawnMetadata:
    """Tests for agent_run_id persistence in prepare_terminal_spawn."""

    def test_calls_update_terminal_pickup_metadata(self) -> None:
        """prepare_terminal_spawn persists agent_run_id to session record."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="machine-1",
            workflow_name="plan-execute",
        )

        assert isinstance(result, PreparedSpawn)
        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name="plan-execute",
        )

    def test_persists_none_workflow(self) -> None:
        """prepare_terminal_spawn passes workflow_name=None when not provided."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="machine-1",
        )

        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name=None,
        )

    def test_agent_run_id_format(self) -> None:
        """agent_run_id starts with 'run-' and is 16 chars (run- + 12 hex)."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="machine-1",
        )

        assert result.agent_run_id.startswith("run-")
        assert len(result.agent_run_id) == 16
        assert re.match(r"^run-[0-9a-f]{12}$", result.agent_run_id)


class TestPrepareCodexSpawnMetadata:
    """Tests for agent_run_id persistence in prepare_codex_spawn_with_preflight."""

    @pytest.mark.asyncio
    async def test_calls_update_terminal_pickup_metadata(self) -> None:
        """prepare_codex_spawn_with_preflight persists agent_run_id."""
        sm = _make_session_manager()

        codex_info = MagicMock()
        codex_info.session_id = "codex-ext-1"
        codex_info.model = "o3"

        with patch(
            "gobby.agents.codex_session.capture_codex_session_id",
            new_callable=AsyncMock,
            return_value=codex_info,
        ):
            result = await prepare_codex_spawn_with_preflight(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="machine-1",
                workflow_name="test-driven",
            )

        assert isinstance(result, PreparedSpawn)
        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name="test-driven",
        )

    @pytest.mark.asyncio
    async def test_persists_none_workflow(self) -> None:
        """prepare_codex_spawn_with_preflight passes workflow_name=None when not provided."""
        sm = _make_session_manager()

        codex_info = MagicMock()
        codex_info.session_id = "codex-ext-2"
        codex_info.model = None

        with patch(
            "gobby.agents.codex_session.capture_codex_session_id",
            new_callable=AsyncMock,
            return_value=codex_info,
        ):
            result = await prepare_codex_spawn_with_preflight(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="machine-1",
            )

        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name=None,
        )
