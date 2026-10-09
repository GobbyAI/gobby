"""Tests for _activate_default_agent with agent_name_override parameter."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("initial", ["x", "default"])
def test_override_never_changes_role(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    initial: str,
) -> None:
    from gobby.storage.definitions import AgentDefinitionManager
    from tests.hooks.test_session_start_reactivation import activation_handler
    from tests.mcp_proxy.tools.test_apply_agent_definition import register_session

    sid = register_session(temp_db, monkeypatch)
    for name in ("default", "x", "y"):
        body = make_agent_definition(name=name, surfaces=["persona"], prompts={"persona": name})
        AgentDefinitionManager(temp_db).create(
            name=name, definition_json=body.model_dump_json(), source="custom"
        )
    SessionVariableManager(temp_db).merge_variables(sid, {"_agent_type": initial})
    result = activation_handler(temp_db)._activate_default_agent(sid, "codex", None, "y")
    expected = "x" if initial == "x" else "y"
    assert result is not None
    assert result.agent_name == expected
    assert SessionVariableManager(temp_db).get_variables(sid)["_agent_type"] == expected
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    if initial == "x":
        assert len(warnings) == 1
        assert sid in warnings[0]
        assert "keeps agent x" in warnings[0]
        assert "ignored role-change override y" in warnings[0]
    else:
        assert warnings == []


def _make_event_handlers() -> EventHandlers:
    """Create an EventHandlers instance with minimal mocked dependencies."""
    from gobby.workflows.state_manager import SessionVariableManager

    session_storage = MagicMock()
    session_storage.db = MagicMock()
    # Make db.fetchall return empty lists so iteration works
    session_storage.db.fetchall.return_value = []
    session_manager = session_storage
    variables = SessionVariableManager(session_storage.db)
    if isinstance(variables, MagicMock):
        variables.get_variable_subset.return_value = {}

    return EventHandlers(
        session_manager=session_manager,
        session_storage=session_storage,
        logger=logging.getLogger("test"),
    )


def _db(handlers: EventHandlers) -> object:
    session_manager = handlers._session_manager
    assert session_manager is not None
    return session_manager.db


def _make_agent_body(name: str = "test-agent") -> AgentDefinitionBody:
    return make_agent_definition(
        name=name, prompts={"agent": "Work."}, workflows=make_agent_workflows()
    )


class TestAgentNameOverride:
    """Tests for the agent_name_override parameter."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_override_skips_config_repository(
        self, mock_resolve: MagicMock, _mock_svm: MagicMock
    ) -> None:
        """When agent_name_override is provided, stored config is not consulted."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("custom-agent")

        with patch("gobby.storage.config_repository.ConfigRepository") as mock_repo:
            handlers._activate_default_agent(
                session_id="sess-1",
                cli_source="claude",
                project_id="proj-1",
                agent_name_override="custom-agent",
            )

            mock_repo.assert_not_called()
            assert mock_repo.call_count == 0
            assert not mock_repo.called

        mock_resolve.assert_called_once_with(
            "custom-agent", _db(handlers), project_id="proj-1", cli_source="claude"
        )
        assert mock_resolve.call_count == 1
        assert mock_resolve.call_args is not None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_no_override_reads_config_repository(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """When no override is provided, the stored default agent is consulted."""
        handlers = _make_event_handlers()
        configured_agent = "configured-default"
        mock_resolve.return_value = _make_agent_body(configured_agent)

        # SessionVariableManager must return empty vars so the code falls
        # through to the config repository instead of using existing _agent_type.
        mock_svm_cls.return_value.get_variable_subset.return_value = {}

        with patch("gobby.storage.config_repository.ConfigRepository") as mock_repo:
            mock_repo.return_value.read.return_value.values = {"default_agent": configured_agent}

            handlers._activate_default_agent(
                session_id="sess-1",
                cli_source="claude",
                project_id=None,
            )

            assert mock_repo.call_count == 1
            assert mock_repo.call_args == call(_db(handlers))
            assert mock_repo.return_value.read.call_count == 1
            assert mock_repo.return_value.read.call_args == call(resolve_secrets=False)
            assert mock_resolve.call_count == 1
            assert mock_resolve.call_args == call(
                configured_agent,
                _db(handlers),
                project_id=None,
                cli_source="claude",
            )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_override_resolves_correct_agent(
        self, mock_resolve: MagicMock, _mock_svm: MagicMock
    ) -> None:
        """The override name is passed directly to resolve_agent."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("my-agent")

        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id="proj-2",
            agent_name_override="my-agent",
        )

        assert mock_resolve.call_count == 1
        assert mock_resolve.call_args == call(
            "my-agent", _db(handlers), project_id="proj-2", cli_source="claude"
        )
        assert mock_resolve.return_value.name == "my-agent"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_override_sets_agent_type_on_session(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """Session should have _agent_type set to the override agent name via SessionVariableManager."""
        handlers = _make_event_handlers()
        agent_body = _make_agent_body("my-agent")
        mock_resolve.return_value = agent_body

        mock_svm = MagicMock()
        mock_svm.get_variable_subset.return_value = {}
        mock_svm_cls.return_value = mock_svm

        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="my-agent",
        )

        # Verify SessionVariableManager.merge_variables was called with _agent_type
        mock_svm.merge_variables.assert_called_once()
        call_args = mock_svm.merge_variables.call_args
        assert call_args[0][0] == "sess-1"
        changes = call_args[0][1]
        assert changes["_agent_type"] == "my-agent"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.storage.definitions.variables.SessionVariableDefaultManager")
    @patch("gobby.storage.definitions.rules.RuleDefinitionManager")
    @patch("gobby.workflows.variable_defaults.resolve_session_project_id")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_resolves_session_project_once_for_agent_rules_and_variables(
        self,
        mock_resolve: MagicMock,
        mock_session_project: MagicMock,
        mock_rules_cls: MagicMock,
        mock_vars_cls: MagicMock,
        _mock_svm: MagicMock,
    ) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("scoped-agent")
        mock_session_project.return_value = "proj-from-session"
        mock_rules_cls.return_value.list_all.return_value = []
        mock_vars_cls.return_value.list_all.return_value = []

        result = handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="scoped-agent",
        )

        assert result is not None
        assert result.agent_name == "scoped-agent"
        mock_session_project.assert_called_once_with(_db(handlers), "sess-1")
        mock_resolve.assert_called_once_with(
            "scoped-agent",
            _db(handlers),
            project_id="proj-from-session",
            cli_source="claude",
        )
        mock_rules_cls.return_value.list_all.assert_called_once_with(
            enabled=True,
            project_id="proj-from-session",
        )
        mock_vars_cls.return_value.list_all.assert_called_once_with(
            project_id="proj-from-session",
            enabled=True,
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.storage.definitions.variables.SessionVariableDefaultManager")
    @patch("gobby.storage.definitions.rules.RuleDefinitionManager")
    @patch("gobby.workflows.variable_defaults.resolve_session_project_id")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_caller_project_id_is_not_re_resolved(
        self,
        mock_resolve: MagicMock,
        mock_session_project: MagicMock,
        mock_rules_cls: MagicMock,
        mock_vars_cls: MagicMock,
        _mock_svm: MagicMock,
    ) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("scoped-agent")
        mock_rules_cls.return_value.list_all.return_value = []
        mock_vars_cls.return_value.list_all.return_value = []

        result = handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id="proj-1",
            agent_name_override="scoped-agent",
        )

        assert result is not None
        assert result.agent_name == "scoped-agent"
        mock_session_project.assert_not_called()
        mock_resolve.assert_called_once_with(
            "scoped-agent",
            _db(handlers),
            project_id="proj-1",
            cli_source="claude",
        )
        mock_rules_cls.return_value.list_all.assert_called_once_with(
            enabled=True,
            project_id="proj-1",
        )


class TestActivateDefaultAgentEdgeCases:
    """Edge cases for _activate_default_agent."""

    def test_no_session_manager_returns_early(self) -> None:
        """Without a session manager (or its alias), activation returns None."""
        handlers = EventHandlers(
            session_manager=None,
            session_storage=None,
            logger=logging.getLogger("test"),
        )

        result = handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="my-agent",
        )
        assert result is None

    def test_session_storage_aliases_session_manager(self) -> None:
        """session_storage is a compatibility alias feeding _session_manager."""
        storage = MagicMock()
        handlers = EventHandlers(
            session_manager=None,
            session_storage=storage,
            logger=logging.getLogger("test"),
        )

        assert handlers._session_manager is storage

    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_override_none_agent_name_skips(self, mock_resolve: MagicMock) -> None:
        """When override is 'none', method returns early without resolving."""
        handlers = _make_event_handlers()

        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="none",
        )

        mock_resolve.assert_not_called()
        assert mock_resolve.call_count == 0
        assert not mock_resolve.called

    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_resolve_failure_logs_error(self, mock_resolve: MagicMock) -> None:
        """AgentResolutionError should be caught and logged."""
        from gobby.workflows.agent_resolver import AgentResolutionError

        handlers = _make_event_handlers()
        mock_resolve.side_effect = AgentResolutionError("not found")

        result = handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="bad-agent",
        )
        assert result is None

    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_resolve_returns_none_logs_debug(self, mock_resolve: MagicMock) -> None:
        """When resolve_agent returns None, method logs and returns."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = None

        # Should not raise
        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="missing-agent",
        )

        session_manager = handlers._session_manager
        assert session_manager is not None
        update = cast(MagicMock, session_manager.update)
        update.assert_not_called()
        assert update.call_count == 0
