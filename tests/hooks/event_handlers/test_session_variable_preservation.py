"""Tests for variable preservation across compact/restart in _activate_default_agent.

On compact/restart, _activate_default_agent re-runs. It must NOT overwrite
user-facing variables that were set during the session, but it MUST re-apply
internal/metadata keys that reflect current agent configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = [pytest.mark.unit]

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _make_event_handlers() -> EventHandlers:
    """Create an EventHandlers instance with minimal mocked dependencies."""
    session_storage = MagicMock()
    session_storage.db = MagicMock()
    session_storage.db.fetchall.return_value = []
    session_manager = session_storage

    return EventHandlers(
        session_manager=session_manager,
        session_storage=session_storage,
        logger=logging.getLogger("test"),
    )


def _make_agent_body(
    name: str = "default",
    variables: dict | None = None,
) -> MagicMock:
    """Create a mock agent body with optional default variables."""
    body = MagicMock()
    body.name = name
    body.prompt_for.return_value = None
    body.workflows = MagicMock()
    body.workflows.skill_format = None
    body.workflows.variables = variables
    body.workflows.rules = []
    body.workflows.skills = []
    body.workflows.rule_selectors = None
    body.rules = []
    body.skills = []
    body.variables = None
    body.blocked_tools = []
    body.blocked_mcp_tools = []
    body.steps = None
    body.step_variables = {}
    body.step_workflow = None
    return body


def _get_merged_changes(mock_svm: MagicMock) -> dict:
    """Extract the changes dict passed to merge_variables."""
    mock_svm.merge_variables.assert_called_once()
    return mock_svm.merge_variables.call_args[0][1]


def _make_hook_event(data: dict | None = None, external_id: str = "external-1") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata={},
    )


def _make_project(db: HubDatabase, tmp_path: Path) -> str:
    project = LocalProjectManager(db).create(name="variable-preservation", repo_path=str(tmp_path))
    return project.id


def _make_real_event_handlers(
    db: HubDatabase,
    project_id: str,
) -> EventHandlers:
    return EventHandlers(
        session_manager=SessionManager(db),  # type: ignore[arg-type]
        get_machine_id=lambda: "21000000-0000-4000-8000-000000000001",
        resolve_project_id=lambda _project_id, _cwd: project_id,
        logger=logging.getLogger("test"),
    )


def _register_session(db: HubDatabase, project_id: str, tmp_path: Path) -> str:
    return SessionManager(db).register_session(
        external_id="external-activation",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project_id,
        project_path=str(tmp_path),
    )


def test_reconstructed_daemon_managers_preserve_schema_leases(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _make_project(temp_db, tmp_path)
    session_id = _register_session(temp_db, project_id, tmp_path)
    SessionVariableManager(temp_db).merge_variables(
        session_id,
        {"unlocked_tools": ["gobby-tasks:create_task"]},
    )

    reconstructed_session_manager = SessionManager(temp_db)
    reconstructed_variables = SessionVariableManager(
        reconstructed_session_manager.db
    ).get_variables(session_id)

    assert reconstructed_variables["unlocked_tools"] == ["gobby-tasks:create_task"]


def test_gobby_session_id_binding_merges_terminal_context(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _make_project(temp_db, tmp_path)
    session_manager = SessionManager(temp_db)
    session = session_manager.register(
        external_id="gobby-pre-created",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=project_id,
        terminal_context={
            "tmux_pane": "%42",
            "tmux_socket_path": "/tmp/tmux.sock",
            "cwd": "/work/old",
            "gobby_agent_run_id": "run-existing",
        },
    )
    handlers = _make_real_event_handlers(temp_db, project_id)
    event = _make_hook_event(
        {
            "skip_default_agent_activation": True,
            "terminal_context": {
                "gobby_session_id": session.id,
                "tmux_pane": None,
                "cwd": "/work/new",
                "parent_pid": 1234,
            },
        },
        external_id="native-codex-session",
    )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
        patch("gobby.hooks.event_handlers._session_start.flow._seed_parent_turn_seq"),
        patch("gobby.hooks.event_handlers._session_start.flow._seed_wiki_overview_var"),
        patch("gobby.hooks.event_handlers._session_start.flow.seed_user_profile_content"),
        patch(
            "gobby.hooks.event_handlers._session_start.flow."
            "_schedule_tmux_window_rename_for_session"
        ),
        patch("gobby.hooks.terminal_context._record_parent_process_identity"),
    ):
        response = handlers.handle_session_start(event)

    assert response.decision == "allow"
    updated = session_manager.get(session.id)
    assert updated is not None
    assert updated.external_id == "native-codex-session"
    assert updated.terminal_context == {
        "tmux_pane": "%42",
        "tmux_socket_path": "/tmp/tmux.sock",
        "cwd": "/work/new",
        "gobby_agent_run_id": "run-existing",
        "gobby_session_id": session.id,
        "parent_pid": 1234,
    }


class TestNewSessionGetsAllDefaults:
    """Brand new sessions (no existing variables) get every default applied."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_empty_session_receives_all_changes(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(
            variables={"mode_level": 2, "stop_attempts": 0}
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {}  # New session — no existing vars

        handlers._activate_default_agent(
            session_id="sess-new",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        assert "_agent_type" in changes
        assert "mode_level" in changes
        assert changes["mode_level"] == 2
        assert changes["stop_attempts"] == 0


class TestReturningSessionPreservesUserVariables:
    """Compact/restart must NOT overwrite user-facing variables already set."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_preserves_mode_level(self, mock_resolve: MagicMock, mock_svm_cls: MagicMock) -> None:
        """User-tuned mode_level should not reset on compact."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(variables={"mode_level": 2})

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "mode_level": 1,
            "task_has_commits": True,
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        # mode_level already exists -> must NOT be overwritten
        assert "mode_level" not in changes

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_preserves_stop_attempts(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """stop_attempts set during session should not be reset to default."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(variables={"stop_attempts": 0})

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "stop_attempts": 3,  # Incremented during session
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        assert "stop_attempts" not in changes

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_preserves_all_existing_user_variables(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """No existing user-facing variable should be overwritten."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(
            variables={
                "stop_attempts": 0,
                "mode_level": 1,
                "chat_mode": "bypass",
            }
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "stop_attempts": 5,
            "mode_level": 3,
            "chat_mode": "normal",
            "task_has_commits": True,  # Not in defaults, but exists
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        for user_var in ("stop_attempts", "mode_level", "chat_mode"):
            assert user_var not in changes, f"{user_var} should NOT be overwritten"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_ordinary_resume_preserves_schema_leases(
        self,
        mock_resolve: MagicMock,
        mock_svm_cls: MagicMock,
    ) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(variables={"unlocked_tools": []})
        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "unlocked_tools": ["gobby-tasks:create_task"],
        }

        handlers._activate_default_agent(
            session_id="sess-resume",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        assert "unlocked_tools" not in _get_merged_changes(mock_svm)


class TestReturningSessionReappliesInternalKeys:
    """Internal/metadata keys must always be re-applied on compact/restart."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_reapplies_agent_type(self, mock_resolve: MagicMock, mock_svm_cls: MagicMock) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("default")

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "mode_level": 1,
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        assert changes["_agent_type"] == "default"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_reapplies_all_internal_keys(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """All _ALWAYS_REAPPLY keys should be present even when they already exist."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("default")

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "old-agent",
            "_active_rule_names": ["old-rule"],
            "is_spawned_agent": False,
            "mode_level": 1,
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        # Internal keys always re-applied
        assert "_agent_type" in changes
        assert "_active_rule_names" in changes
        assert "is_spawned_agent" in changes
        # User variable preserved
        assert "mode_level" not in changes


class TestMixedNewAndExistingVariables:
    """New defaults (not yet in session) should still be applied."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_new_defaults_added_existing_preserved(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """Variables not yet in session get their defaults; existing ones are kept."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(
            variables={
                "mode_level": 2,  # Already exists -> skip
                "brand_new_variable": "hello",  # Not in session → apply
            }
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "mode_level": 1,
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        assert "brand_new_variable" in changes
        assert changes["brand_new_variable"] == "hello"
        assert "mode_level" not in changes

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_removed_memory_recall_helper_flag_counts_as_regular_variable(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """The removed helper flag is no longer treated as an internal key."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(
            variables={
                "memory_recall_helper_enabled": True,
                "visible_variable": "hello",
            }
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {}

        result = handlers._activate_default_agent(
            session_id="sess-new",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        assert result is not None
        assert result.variables_count == 2


@patch("gobby.workflows.agent_resolver.resolve_agent")
def test_parent_turn_seq_preserved_across_activation(
    mock_resolve: MagicMock,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A re-activation must preserve the runtime-incremented parent counter."""
    project_id = _make_project(temp_db, tmp_path)
    session_id = _register_session(temp_db, project_id, tmp_path)
    SessionVariableManager(temp_db).merge_variables(session_id, {"parent_turn_seq": 42})
    handlers = _make_real_event_handlers(temp_db, project_id)
    mock_resolve.return_value = _make_agent_body(variables={"mode_level": 1})

    handlers._activate_default_agent(
        session_id=session_id,
        cli_source="claude",
        project_id=project_id,
        agent_name_override="default",
    )

    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["parent_turn_seq"] == 42


@patch("gobby.workflows.agent_resolver.resolve_agent")
def test_parent_turn_seq_seeded_on_first_activation(
    mock_resolve: MagicMock,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """First activation seeds parent_turn_seq when it is absent from existing variables."""
    project_id = _make_project(temp_db, tmp_path)
    handlers = _make_real_event_handlers(temp_db, project_id)
    mock_resolve.return_value = _make_agent_body(variables={"mode_level": 1})
    event = _make_hook_event(
        {"cwd": str(tmp_path), "project_id": project_id, "agent_name_override": "default"}
    )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
    ):
        handlers.handle_session_start(event)

    session_id = event.metadata["_platform_session_id"]
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["parent_turn_seq"] == 0
    assert "memory_recall_helper_enabled" not in variables


def test_variables_seeded_when_activation_skipped_at_flow_level(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Skipped default-agent activation must still seed parent turn tracking."""
    project_id = _make_project(temp_db, tmp_path)
    handlers = _make_real_event_handlers(temp_db, project_id)
    event = _make_hook_event(
        {"cwd": str(tmp_path), "project_id": project_id, "skip_default_agent_activation": True}
    )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent", return_value=None) as activate,
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
    ):
        handlers.handle_session_start(event)

    activate.assert_not_called()
    session_id = event.metadata["_platform_session_id"]
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["parent_turn_seq"] == 0
    assert "memory_recall_helper_enabled" not in variables


def test_full_session_start_marks_startup_context_injected(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Full startup context records durable evidence for later live resumes."""
    project_id = _make_project(temp_db, tmp_path)
    handlers = _make_real_event_handlers(temp_db, project_id)
    event = _make_hook_event(
        {
            "cwd": str(tmp_path),
            "project_id": project_id,
            "skip_default_agent_activation": True,
        }
    )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent", return_value=None) as activate,
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
    ):
        handlers.handle_session_start(event)

    activate.assert_not_called()
    session_id = event.metadata["_platform_session_id"]
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    session = SessionManager(temp_db).get(session_id)
    assert variables["_startup_context_injected"] is True
    assert session is not None
    assert session.context_injected is True


def test_variables_seeded_in_pre_created_session_flow(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Pre-created sessions seed parent turn tracking before activation runs."""
    project_id = _make_project(temp_db, tmp_path)
    session_id = _register_session(temp_db, project_id, tmp_path)
    session_manager = SessionManager(temp_db)
    existing_session = session_manager.get(session_id)
    assert existing_session is not None
    handlers = EventHandlers(
        session_manager=session_manager,  # type: ignore[arg-type]
        get_machine_id=lambda: "21000000-0000-4000-8000-000000000001",
        logger=logging.getLogger("test"),
    )
    event = _make_hook_event({"agent_name_override": "default"}, external_id="external-pre")
    seen_during_activation: dict = {}

    def capture_seeded_variables(
        activation_session_id: str,
        _cli_source: str,
        _project_id: str | None,
        *,
        agent_name_override: str | None = None,
    ) -> None:
        assert agent_name_override == "default"
        seen_during_activation.update(
            SessionVariableManager(temp_db).get_variables(activation_session_id)
        )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent", side_effect=capture_seeded_variables),
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
    ):
        handlers._handle_pre_created_session(
            existing_session=existing_session,
            external_id="external-pre",
            transcript_path=None,
            cli_source="claude",
            event=event,
            cwd=str(tmp_path),
        )

    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert seen_during_activation["parent_turn_seq"] == 0
    assert "memory_recall_helper_enabled" not in seen_during_activation
    assert variables["parent_turn_seq"] == 0
    assert "memory_recall_helper_enabled" not in variables


def test_pre_created_web_chat_seeds_profile_when_activation_skipped(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Web-chat pickup seeds profile content without forcing default-agent activation."""
    project_id = _make_project(temp_db, tmp_path)
    session_id = _register_session(temp_db, project_id, tmp_path)
    session_manager = SessionManager(temp_db)
    session_manager.update(session_id, session_type="web_chat")
    existing_session = session_manager.get(session_id)
    assert existing_session is not None
    assert existing_session.session_type == "web_chat"

    handlers = EventHandlers(
        session_manager=session_manager,  # type: ignore[arg-type]
        get_machine_id=lambda: "21000000-0000-4000-8000-000000000001",
        logger=logging.getLogger("test"),
    )
    event = _make_hook_event(
        {"skip_default_agent_activation": True},
        external_id="external-pre",
    )

    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent", return_value=None) as activate,
        patch(
            "gobby.hooks.event_handlers._session_start.profile.read_user_profile_content",
            return_value="Prefers concise status updates.",
        ),
        patch.object(
            handlers,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
    ):
        handlers._handle_pre_created_session(
            existing_session=existing_session,
            external_id="external-pre",
            transcript_path=None,
            cli_source="claude",
            event=event,
            cwd=str(tmp_path),
        )

    activate.assert_not_called()
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["user_profile_content"] == "Prefers concise status updates."
