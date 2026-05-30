"""
Tests for terminal spawn prepare functions.

Verifies that prepare_terminal_spawn persists agent_run_id via
update_terminal_pickup_metadata.
"""

import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.agents.constants import UV_CACHE_DIR
from gobby.agents.resume_metadata import json_safe
from gobby.agents.spawn import (
    PreparedSpawn,
    prepare_terminal_spawn,
)
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, managed_tool_bin_dir

pytestmark = pytest.mark.unit


def test_json_safe_sorts_set_values() -> None:
    assert json_safe({"beta", "alpha"}) == ["alpha", "beta"]
    assert json_safe({("beta", 2), ("alpha", 1)}) == [["alpha", 1], ["beta", 2]]


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

        uv_cache = Path(result.env_vars[UV_CACHE_DIR])
        assert uv_cache.parts[-3:-1] == ("gobby", "uv-cache")
        assert uv_cache.parts[-1].startswith("child-sess-1-")
        assert uv_cache.is_dir()

    def test_env_includes_managed_tool_bin_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """prepare_terminal_spawn forwards ~/.gobby/bin through the child env."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")
        sm = _make_session_manager(child_session_id="child-sess-1")

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="machine-1",
        )

        assert result.env_vars[PATH_ENV_VAR].split(os.pathsep) == [
            managed_tool_bin_dir(),
            "/usr/bin",
        ]
