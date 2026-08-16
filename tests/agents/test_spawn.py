"""
Tests for terminal spawn prepare functions.

Verifies that prepare_terminal_spawn persists agent_run_id via
update_terminal_pickup_metadata.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.constants import UV_CACHE_DIR
from gobby.agents.spawn import (
    PreparedSpawn,
    prepare_terminal_spawn,
)
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, managed_tool_bin_dir

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

    @pytest.fixture(autouse=True)
    def _stub_agent_run_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests pin the pickup-metadata contract, not run storage.
        # The real LocalAgentRunManager would decode AgentRun rows from the
        # mocked db, and datetime normalization rejects Mock values.
        monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", MagicMock())

    def test_calls_update_terminal_pickup_metadata(self) -> None:
        """prepare_terminal_spawn persists agent_run_id to session record."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            workflow_name="plan-execute",
        )

        assert isinstance(result, PreparedSpawn)
        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name="plan-execute",
        )

    def test_agent_run_creation_log_is_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        sm = _make_session_manager()

        with caplog.at_level("DEBUG", logger="agents.spawn.prepare_terminal_spawn"):
            prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="21000000-0000-4000-8000-000000000001",
                workflow_name="plan-execute",
            )

        spawn_records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Creating agent_run")
        ]
        assert len(spawn_records) == 1
        assert spawn_records[0].levelname == "DEBUG"

    def test_persists_none_workflow(self) -> None:
        """prepare_terminal_spawn passes workflow_name=None when not provided."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="21000000-0000-4000-8000-000000000001",
        )

        sm.update_terminal_pickup_metadata.assert_called_once_with(
            session_id="child-sess-1",
            agent_run_id=result.agent_run_id,
            workflow_name=None,
        )
        assert sm.update_terminal_pickup_metadata.call_count == 1
        assert sm.update_terminal_pickup_metadata.call_args is not None

    def test_agent_run_id_format(self) -> None:
        """agent_run_id is a canonical uuid string."""
        sm = _make_session_manager()

        result = prepare_terminal_spawn(
            session_manager=sm,
            parent_session_id="parent-1",
            project_id="proj-1",
            machine_id="21000000-0000-4000-8000-000000000001",
        )

        assert str(uuid.UUID(result.agent_run_id)) == result.agent_run_id

    @pytest.mark.parametrize(
        ("timeout_seconds", "expected_lifetime_seconds"),
        [
            (7200.0, 7500.0),
            (None, 3840.0),
            (0.0, 300.0),
            (-300.0, 1.0),
        ],
    )
    def test_issues_managed_credential_before_returning_provider_environment(
        self,
        timeout_seconds: float | None,
        expected_lifetime_seconds: float,
    ) -> None:
        session_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        issued_at = datetime(2026, 8, 12, tzinfo=UTC)
        sm = _make_session_manager(child_session_id=session_id)
        credential_manager = MagicMock()
        credential_manager.issue.return_value.bootstrap_path = Path(
            "/private/runtime/bootstrap.json"
        )
        sm._storage.db.managed_credential_manager = credential_manager

        with patch("gobby.agents.spawn.datetime") as mock_datetime:
            mock_datetime.now.return_value = issued_at
            result = prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id=str(uuid.uuid4()),
                project_id=project_id,
                machine_id=str(uuid.uuid4()),
                agent_run_id=run_id,
                timeout_seconds=timeout_seconds,
            )

        credential_manager.issue.assert_called_once_with(
            managed_execution_id=uuid.UUID(run_id),
            owner_kind="agent_run",
            session_id=uuid.UUID(session_id),
            agent_run_id=uuid.UUID(run_id),
            expires_at=issued_at + timedelta(seconds=expected_lifetime_seconds),
        )
        assert result.env_vars["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == (
            "/private/runtime/bootstrap.json"
        )
        assert result.managed_credential is credential_manager.issue.return_value

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
            machine_id="21000000-0000-4000-8000-000000000001",
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
            machine_id="21000000-0000-4000-8000-000000000001",
        )

        assert result.env_vars[PATH_ENV_VAR].split(os.pathsep) == [
            managed_tool_bin_dir(),
            "/usr/bin",
        ]


class TestPrepareTerminalSpawnCleanup:
    """Fault injection at each pre-launch acquisition tears down partial state."""

    @pytest.fixture(autouse=True)
    def _stub_agent_run_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", MagicMock())

    def test_fault_after_child_session_is_cleaned_up(self) -> None:
        sm = _make_session_manager(child_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
                side_effect=RuntimeError("vars failed"),
            ),
            patch("gobby.agents.spawn.cleanup_unlaunched_spawn") as cleanup,
            pytest.raises(RuntimeError, match="vars failed"),
        ):
            prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="21000000-0000-4000-8000-000000000001",
                initial_variables={"k": "v"},
            )
        cleanup.assert_called_once()
        assert cleanup.call_args.kwargs["session_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def test_fault_after_run_prepare_is_cleaned_up(self) -> None:
        sm = _make_session_manager(child_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with (
            patch(
                "gobby.agents.spawn._prepare_run_for_session",
                side_effect=RuntimeError("run failed"),
            ),
            patch("gobby.agents.spawn.cleanup_unlaunched_spawn") as cleanup,
            pytest.raises(RuntimeError, match="run failed"),
        ):
            prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="21000000-0000-4000-8000-000000000001",
            )
        cleanup.assert_called_once()
        assert cleanup.call_args.kwargs["session_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def test_fault_after_credential_is_cleaned_up(self) -> None:
        sm = _make_session_manager(child_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        prepared = PreparedSpawn(
            session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            agent_run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            parent_session_id="parent-1",
            project_id="proj-1",
            workflow_name=None,
            agent_depth=1,
            env_vars={},
        )
        with (
            patch("gobby.agents.spawn._prepare_run_for_session", return_value=prepared),
            patch(
                "gobby.agents.spawn._issue_prelaunch_credential",
                side_effect=RuntimeError("cred failed"),
            ),
            patch("gobby.agents.spawn.cleanup_unlaunched_spawn") as cleanup,
            pytest.raises(RuntimeError, match="cred failed"),
        ):
            prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id="parent-1",
                project_id="proj-1",
                machine_id="21000000-0000-4000-8000-000000000001",
            )
        cleanup.assert_called_once()
        assert cleanup.call_args.kwargs["session_id"] == prepared.session_id
        assert cleanup.call_args.kwargs["agent_run_id"] == prepared.agent_run_id
