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
    _issue_prelaunch_credential,
    prepare_terminal_spawn,
)
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, managed_tool_bin_dir
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV

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
        sm._storage.get.return_value.machine_id = "21000000-0000-4000-8000-000000000001"
        credential_manager = MagicMock()
        credential = credential_manager.issue.return_value
        credential.bootstrap_path = Path("/private/runtime/bootstrap.json")
        credential.expires_at = issued_at + timedelta(seconds=expected_lifetime_seconds)
        launch = MagicMock()
        launch.grant_path = Path("/private/runtime/grant.json")

        with (
            patch("gobby.agents.spawn.datetime") as mock_datetime,
            patch("gobby.agents.spawn.read_local_api_token", return_value="op-token"),
            patch("gobby.agents.code_index._active_deployment_grant_context"),
            patch("gobby.agents.code_index._signed_grant_from_credential"),
            patch(
                "gobby.runtime_grants.launch.materialize_managed_launch",
                return_value=launch,
            ),
        ):
            mock_datetime.now.return_value = issued_at
            result = prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id=str(uuid.uuid4()),
                project_id=project_id,
                machine_id=str(uuid.uuid4()),
                agent_run_id=run_id,
                timeout_seconds=timeout_seconds,
                credential_manager=credential_manager,
            )

        credential_manager.issue.assert_called_once_with(
            managed_execution_id=uuid.UUID(run_id),
            owner_kind="agent_run",
            session_id=uuid.UUID(session_id),
            agent_run_id=uuid.UUID(run_id),
            expires_at=issued_at + timedelta(seconds=expected_lifetime_seconds),
        )
        assert result.env_vars["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == (
            "/private/runtime/grant.json"
        )
        assert result.managed_credential is credential_manager.issue.return_value

    def test_persists_isolation_workspace_on_run_before_issuing_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run row carries its worktree before the managed credential binds it."""
        session_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        sm = _make_session_manager(child_session_id=session_id)
        sm._storage.get.return_value.machine_id = "21000000-0000-4000-8000-000000000001"
        credential_manager = MagicMock()
        credential = MagicMock()
        credential.bootstrap_path = Path("/private/runtime/bootstrap.json")
        credential.expires_at = datetime(2026, 8, 12, tzinfo=UTC) + timedelta(hours=1)
        run_manager_cls = MagicMock()
        monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", run_manager_cls)
        order: list[str] = []

        def record_create(**_: object) -> None:
            order.append("create")

        def record_issue(**_: object) -> MagicMock:
            order.append("issue")
            return credential

        run_manager_cls.return_value.create.side_effect = record_create
        credential_manager.issue.side_effect = record_issue
        launch = MagicMock()
        launch.grant_path = Path("/private/runtime/grant.json")

        with (
            patch("gobby.agents.spawn.read_local_api_token", return_value="op-token"),
            patch("gobby.agents.code_index._active_deployment_grant_context"),
            patch("gobby.agents.code_index._signed_grant_from_credential"),
            patch(
                "gobby.runtime_grants.launch.materialize_managed_launch",
                return_value=launch,
            ),
        ):
            prepare_terminal_spawn(
                session_manager=sm,
                parent_session_id=str(uuid.uuid4()),
                project_id=str(uuid.uuid4()),
                machine_id=str(uuid.uuid4()),
                agent_run_id=run_id,
                worktree_id="wt-1",
                clone_id=None,
                credential_manager=credential_manager,
            )

        assert order == ["create", "issue"]
        create_kwargs = run_manager_cls.return_value.create.call_args.kwargs
        assert create_kwargs["run_id"] == run_id
        assert create_kwargs["worktree_id"] == "wt-1"
        assert create_kwargs["clone_id"] is None

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


class TestPrepareRunForSessionPromptCleanup:
    """Prompt files created inside _prepare_run_for_session must not leak on failure."""

    def test_failed_prepare_deletes_prompt_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.agents.spawn import MAX_ENV_PROMPT_LENGTH, _prepare_run_for_session

        sm = _make_session_manager()
        prompt_path = tmp_path / "prompt-child-sess-1.txt"
        monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", MagicMock())

        def _create_prompt(prompt: str, session_id: str) -> str:
            prompt_path.write_text(prompt, encoding="utf-8")
            return str(prompt_path)

        monkeypatch.setattr("gobby.agents.spawn.create_prompt_file", _create_prompt)
        monkeypatch.setattr(
            "gobby.agents.spawn.get_terminal_env_vars",
            MagicMock(side_effect=RuntimeError("env failed")),
        )

        with pytest.raises(RuntimeError, match="env failed"):
            _prepare_run_for_session(
                session_manager=sm,
                session_id="child-sess-1",
                session_depth=1,
                session_seq_num=1,
                parent_session_id="parent-1",
                project_id="proj-1",
                provider="claude",
                workflow_name=None,
                agent_name=None,
                git_branch=None,
                prompt="x" * (MAX_ENV_PROMPT_LENGTH + 1),
                model=None,
                is_local=False,
                max_agent_depth=5,
                agent_run_id="run-1",
                task_id=None,
                claimed_session_id=None,
                timeout_seconds=None,
                sandbox_enabled=False,
                requested_reasoning_effort=None,
                effective_reasoning_effort=None,
                reasoning_required=False,
                reasoning_status="not_requested",
                reasoning_message=None,
                resume_metadata_json=None,
                bind_run=MagicMock(),
                worktree_id=None,
                clone_id=None,
            )

        assert not prompt_path.exists()


class TestIssuePrelaunchCredential:
    """The managed bootstrap env must point at a signed grant file."""

    @staticmethod
    def _prepared() -> PreparedSpawn:
        return PreparedSpawn(
            session_id=str(uuid.uuid4()),
            agent_run_id=str(uuid.uuid4()),
            parent_session_id="parent-1",
            project_id="proj-1",
            workflow_name=None,
            agent_depth=1,
            env_vars={},
        )

    @staticmethod
    def _session_manager_with_credential(tmp_path: Path) -> tuple[MagicMock, MagicMock]:
        sm = MagicMock()
        manager = MagicMock()
        credential = MagicMock()
        credential.bootstrap_path = tmp_path / "run" / "credentials.json"
        credential.bootstrap_path.parent.mkdir(parents=True)
        credential.expires_at = datetime.now(UTC) + timedelta(hours=1)
        manager.issue.return_value = credential
        session = MagicMock()
        session.machine_id = "21000000-0000-4000-8000-000000000001"
        sm._storage.get.return_value = session
        return sm, manager

    def test_bootstrap_env_points_at_materialized_grant_file(self, tmp_path: Path) -> None:
        sm, manager = self._session_manager_with_credential(tmp_path)
        prepared = self._prepared()
        launch = MagicMock()
        launch.grant_path = tmp_path / "run" / "grant.json"

        with (
            patch("gobby.agents.spawn.read_local_api_token", return_value="op-token"),
            patch("gobby.agents.code_index._active_deployment_grant_context") as mock_context,
            patch("gobby.agents.code_index._signed_grant_from_credential") as mock_signer,
            patch(
                "gobby.runtime_grants.launch.materialize_managed_launch",
                return_value=launch,
            ) as mock_materialize,
        ):
            result = _issue_prelaunch_credential(
                sm,
                prepared,
                timeout_seconds=None,
                credential_manager=manager,
            )

        assert result.env_vars[MANAGED_EXECUTION_BOOTSTRAP_ENV] == str(launch.grant_path)
        assert result.managed_credential is manager.issue.return_value
        mock_signer.assert_called_once_with(
            manager.issue.return_value,
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="proj-1",
            session_id=prepared.session_id,
            context=mock_context.return_value,
        )
        materialize_kwargs = mock_materialize.call_args.kwargs
        assert materialize_kwargs["dest_dir"] == tmp_path / "run"
        assert materialize_kwargs["operator_token"] == "op-token"
        assert materialize_kwargs["deadline_seconds"] > 0

    def test_missing_operator_token_fails_closed_before_issuing(self, tmp_path: Path) -> None:
        sm, manager = self._session_manager_with_credential(tmp_path)

        with (
            patch("gobby.agents.spawn.read_local_api_token", return_value=None),
            pytest.raises(RuntimeError, match="operator token"),
        ):
            _issue_prelaunch_credential(
                sm,
                self._prepared(),
                timeout_seconds=None,
                credential_manager=manager,
            )

        manager.issue.assert_not_called()

    def test_missing_lease_context_fails_closed_before_issuing(self, tmp_path: Path) -> None:
        sm, manager = self._session_manager_with_credential(tmp_path)

        with (
            patch("gobby.agents.spawn.read_local_api_token", return_value="op-token"),
            patch(
                "gobby.agents.code_index._active_deployment_grant_context",
                side_effect=RuntimeError("lease_unavailable"),
            ),
            pytest.raises(RuntimeError, match="lease_unavailable"),
        ):
            _issue_prelaunch_credential(
                sm,
                self._prepared(),
                timeout_seconds=None,
                credential_manager=manager,
            )

        manager.issue.assert_not_called()

    def test_no_credential_manager_leaves_prepared_untouched(self) -> None:
        class _BareDb:
            pass

        sm = MagicMock()
        sm._storage.db = _BareDb()
        prepared = self._prepared()

        result = _issue_prelaunch_credential(
            sm,
            prepared,
            timeout_seconds=None,
            credential_manager=None,
        )

        assert result is prepared
        assert MANAGED_EXECUTION_BOOTSTRAP_ENV not in result.env_vars
