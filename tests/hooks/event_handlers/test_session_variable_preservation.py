"""Tests for variable preservation across compact/restart in _activate_default_agent.

On compact/restart, _activate_default_agent re-runs. It must NOT overwrite
user-facing variables (e.g., errors_resolved) that were set during
the session, but it MUST re-apply internal/metadata keys that reflect current
agent configuration.
"""

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
    body.build_prompt_preamble.return_value = None
    body.workflows = MagicMock()
    body.workflows.skill_format = None
    body.workflows.variables = variables
    body.workflows.rules = []
    body.workflows.skills = []
    body.workflows.rule_selectors = None
    body.rules = []
    body.skills = []
    body.variables = None
    body.steps = None
    body.step_variables = {}
    return body


def _get_merged_changes(mock_svm: MagicMock) -> dict:
    """Extract the changes dict passed to merge_variables."""
    mock_svm.merge_variables.assert_called_once()
    return mock_svm.merge_variables.call_args[0][1]


class TestNewSessionGetsAllDefaults:
    """Brand new sessions (no existing variables) get every default applied."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_empty_session_receives_all_changes(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(
            variables={"errors_resolved": False, "stop_attempts": 0}
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
        assert "errors_resolved" in changes
        assert changes["errors_resolved"] is False
        assert changes["stop_attempts"] == 0


class TestReturningSessionPreservesUserVariables:
    """Compact/restart must NOT overwrite user-facing variables already set."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.workflows.agent_resolver.resolve_agent")
    def test_preserves_errors_resolved(
        self, mock_resolve: MagicMock, mock_svm_cls: MagicMock
    ) -> None:
        """The exact bug scenario: triaged=true gets reset to false on compact."""
        handlers = _make_event_handlers()
        mock_resolve.return_value = _make_agent_body(variables={"errors_resolved": False})

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "errors_resolved": True,  # Set by agent during session
            "task_has_commits": True,
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        # errors_resolved already exists → must NOT be overwritten
        assert "errors_resolved" not in changes

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
                "errors_resolved": False,
                "stop_attempts": 0,
                "mode_level": 1,
            }
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "errors_resolved": True,
            "stop_attempts": 5,
            "mode_level": 3,
            "task_has_commits": True,  # Not in defaults, but exists
        }

        handlers._activate_default_agent(
            session_id="sess-compact",
            cli_source="claude",
            project_id=None,
            agent_name_override="default",
        )

        changes = _get_merged_changes(mock_svm)
        for user_var in ("errors_resolved", "stop_attempts", "mode_level"):
            assert user_var not in changes, f"{user_var} should NOT be overwritten"


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
            "errors_resolved": True,
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
            "errors_resolved": True,
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
        assert "errors_resolved" not in changes


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
                "errors_resolved": False,  # Already exists → skip
                "brand_new_variable": "hello",  # Not in session → apply
            }
        )

        mock_svm = MagicMock()
        mock_svm_cls.return_value = mock_svm
        mock_svm.get_variables.return_value = {
            "_agent_type": "default",
            "errors_resolved": True,
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
        assert "errors_resolved" not in changes
