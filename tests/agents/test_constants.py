"""Tests for agent constants module."""

import pytest

from gobby.agents.constants import (
    ALL_TERMINAL_ENV_VARS,
    GOBBY_AGENT_DEPTH,
    GOBBY_AGENT_RUN_ID,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_PROJECT_ID,
    GOBBY_PROMPT,
    GOBBY_PROMPT_FILE,
    GOBBY_SESSION_ID,
    GOBBY_WORKFLOW_NAME,
    get_terminal_env_vars,
)

pytestmark = pytest.mark.unit


class TestEnvironmentVariableConstants:
    """Tests for environment variable constant definitions."""

    def test_constants_are_strings(self) -> None:
        """All constants are string values."""
        assert isinstance(GOBBY_SESSION_ID, str)
        assert isinstance(GOBBY_PARENT_SESSION_ID, str)
        assert isinstance(GOBBY_AGENT_RUN_ID, str)
        assert isinstance(GOBBY_WORKFLOW_NAME, str)
        assert isinstance(GOBBY_PROJECT_ID, str)
        assert isinstance(GOBBY_AGENT_DEPTH, str)
        assert isinstance(GOBBY_MAX_AGENT_DEPTH, str)
        assert isinstance(GOBBY_PROMPT, str)
        assert isinstance(GOBBY_PROMPT_FILE, str)

    def test_constants_are_uppercase(self) -> None:
        """All constants follow ENV_VAR naming convention."""
        for var in ALL_TERMINAL_ENV_VARS:
            assert var == var.upper(), f"{var} should be uppercase"

    def test_constants_start_with_gobby(self) -> None:
        """All constants are prefixed with GOBBY_."""
        for var in ALL_TERMINAL_ENV_VARS:
            assert var.startswith("GOBBY_"), f"{var} should start with GOBBY_"

    def test_all_terminal_env_vars_complete(self) -> None:
        """ALL_TERMINAL_ENV_VARS contains all constants."""
        expected = {
            GOBBY_SESSION_ID,
            GOBBY_PARENT_SESSION_ID,
            GOBBY_AGENT_RUN_ID,
            GOBBY_WORKFLOW_NAME,
            GOBBY_PROJECT_ID,
            GOBBY_AGENT_DEPTH,
            GOBBY_MAX_AGENT_DEPTH,
            GOBBY_PROMPT,
            GOBBY_PROMPT_FILE,
        }
        assert set(ALL_TERMINAL_ENV_VARS) == expected


class TestGetTerminalEnvVars:
    """Tests for get_terminal_env_vars function."""

    def test_returns_all_required_vars(self) -> None:
        """Function returns all required environment variables."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[GOBBY_SESSION_ID] == "sess-child"
        assert result[GOBBY_PARENT_SESSION_ID] == "sess-parent"
        assert result[GOBBY_AGENT_RUN_ID] == "run-123"
        assert result[GOBBY_PROJECT_ID] == "proj-abc"

    def test_includes_workflow_when_provided(self) -> None:
        """Function includes workflow name when provided."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name="plan-execute",
        )

        assert result[GOBBY_WORKFLOW_NAME] == "plan-execute"

    def test_omits_workflow_when_none(self) -> None:
        """Function omits workflow name when not provided."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name=None,
        )

        assert GOBBY_WORKFLOW_NAME not in result

    def test_includes_depth_info(self) -> None:
        """Function includes agent depth information."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            agent_depth=2,
            max_agent_depth=5,
        )

        assert result[GOBBY_AGENT_DEPTH] == "2"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_default_depth_values(self) -> None:
        """Function uses default depth values."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[GOBBY_AGENT_DEPTH] == "1"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_omits_parent_session_id_when_empty(self) -> None:
        """Function omits parent_session_id when empty string."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert GOBBY_PARENT_SESSION_ID not in result

    def test_includes_parent_session_id_when_provided(self) -> None:
        """Function includes parent_session_id when non-empty."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[GOBBY_PARENT_SESSION_ID] == "sess-parent"

    def test_all_values_are_strings(self) -> None:
        """Function returns all values as strings."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name="test",
            agent_depth=1,
            max_agent_depth=3,
        )

        for key, value in result.items():
            assert isinstance(value, str), f"Value for {key} should be string"
