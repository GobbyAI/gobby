"""
Tests for terminal spawn prepare functions.

Verifies that prepare_terminal_spawn persists agent_run_id via
update_terminal_pickup_metadata.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.agents.constants import UV_CACHE_DIR
from gobby.agents.spawn import (
    PreparedSpawn,
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
        assert sm.update_terminal_pickup_metadata.call_count == 1
        assert sm.update_terminal_pickup_metadata.call_args is not None

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

    def test_env_includes_spawned_agent_uv_cache_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """prepare_terminal_spawn gives validation commands an isolated uv cache."""
        monkeypatch.setattr("gobby.agents.constants.tempfile.gettempdir", lambda: str(tmp_path))
        sm = _make_session_manager(child_session_id="child/sess-1")

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="machine-1",
        )

        expected = tmp_path / "gobby" / "uv-cache" / "child-sess-1"
        assert result.env_vars[UV_CACHE_DIR] == str(expected)
        assert expected.is_dir()
