"""Tests for _activate_default_agent with agent_name_override parameter."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers

pytestmark = [pytest.mark.unit]


def _make_event_handlers() -> EventHandlers:
    """Create an EventHandlers instance with minimal mocked dependencies."""
    session_storage = MagicMock()
    session_storage.db = MagicMock()
    # Make db.fetchall return empty lists so iteration works
    session_storage.db.fetchall.return_value = []

    session_manager = MagicMock()

    return EventHandlers(
        session_manager=session_manager,
        session_storage=session_storage,
        logger=logging.getLogger("test"),
    )


def _make_agent_body(name: str = "test-agent") -> MagicMock:
    """Create a mock agent body returned by resolve_agent."""
    body = MagicMock()
    body.name = name
    body.build_prompt_preamble.return_value = None
    body.workflows = MagicMock()
    body.workflows.skill_format = None
    body.workflows.variables = None
    body.workflows.rules = []
    body.workflows.skills = []
    body.workflows.rule_selectors = None
    body.rules = []
    body.skills = []
    body.variables = None
    return body


class TestAgentNameOverride:
    """Tests for the agent_name_override parameter."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_override_skips_config_store(
        self, mock_resolve: MagicMock, _mock_svm: MagicMock
    ) -> None:
        """When agent_name_override is provided, ConfigStore should not be consulted."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("custom-agent")

        with patch("gobby.storage.config_store.ConfigStore") as mock_cs:
            handlers._activate_default_agent(
                session_id="sess-1",
                cli_source="claude",
                project_id="proj-1",
                agent_name_override="custom-agent",
            )

            mock_cs.assert_not_called()

        mock_resolve.assert_called_once_with(
            "custom-agent", handlers._session_storage.db, project_id="proj-1"
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_no_override_reads_config_store(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """When no override is provided, ConfigStore is used to get the default agent."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body("default")

        # SessionVariableManager must return empty vars so the code falls
        # through to ConfigStore instead of using existing _agent_type.
        mock_svm_cls.return_value.get_variables.return_value = {}

        with patch("gobby.storage.config_store.ConfigStore") as mock_cs:
            mock_cs.return_value.get.return_value = "default"

            handlers._activate_default_agent(
                session_id="sess-1",
                cli_source="claude",
                project_id=None,
            )

            mock_cs.assert_called_once_with(handlers._session_storage.db)
            mock_cs.return_value.get.assert_called_once_with("default_agent")

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

        mock_resolve.assert_called_once_with(
            "my-agent", handlers._session_storage.db, project_id="proj-2"
        )

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


class TestActivateDefaultAgentEdgeCases:
    """Edge cases for _activate_default_agent."""

    def test_no_session_manager_returns_early(self) -> None:
        """If session_manager is None, method returns without error."""
        handlers = EventHandlers(
            session_manager=None,
            session_storage=MagicMock(),
            logger=logging.getLogger("test"),
        )

        # Should not raise
        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="my-agent",
        )

    def test_no_session_storage_returns_early(self) -> None:
        """If session_storage is None, method returns without error."""
        handlers = EventHandlers(
            session_manager=MagicMock(),
            session_storage=None,
            logger=logging.getLogger("test"),
        )

        # Should not raise
        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="my-agent",
        )

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

    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_resolve_failure_logs_error(self, mock_resolve: MagicMock) -> None:
        """AgentResolutionError should be caught and logged."""
        from gobby.workflows.agent_resolver import AgentResolutionError

        handlers = _make_event_handlers()
        mock_resolve.side_effect = AgentResolutionError("not found")

        # Should not raise
        handlers._activate_default_agent(
            session_id="sess-1",
            cli_source="claude",
            project_id=None,
            agent_name_override="bad-agent",
        )

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

        handlers._session_manager.update.assert_not_called()
