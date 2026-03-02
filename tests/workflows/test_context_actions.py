"""Comprehensive unit tests for context_actions.py.

Tests for context injection, message injection, handoff extraction,
and markdown formatting functions.
"""

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from gobby.workflows.context_actions import (
    format_handoff_as_markdown,
    inject_context,
    inject_message,
)
from gobby.workflows.definitions import WorkflowState

pytestmark = pytest.mark.unit

# --- Fixtures ---


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager."""
    return MagicMock()


@pytest.fixture
def mock_template_engine():
    """Create a mock template engine.

    Default behavior returns "rendered:{template}".
    Tests can override with engine.render.return_value or engine.render.side_effect.
    """
    engine = MagicMock()
    # Use return_value as default - tests can override
    engine.render.return_value = "rendered_content"
    return engine


@pytest.fixture
def workflow_state():
    """Create a workflow state for testing."""
    return WorkflowState(
        session_id="test-session-id",
        workflow_name="test-workflow",
        step="test-step",
        variables={"key": "value"},
    )


@pytest.fixture
def mock_session():
    """Create a mock session object."""
    session = MagicMock()
    session.id = "test-session-id"
    session.parent_session_id = None
    session.summary_markdown = None
    session.compact_markdown = None
    session.jsonl_path = None
    session.project_id = "test-project-id"
    return session


@pytest.fixture
def mock_parent_session():
    """Create a mock parent session object."""
    parent = MagicMock()
    parent.id = "parent-session-id"
    parent.summary_markdown = "Parent session summary content"
    return parent


# --- Tests for inject_context ---


class TestInjectContext:
    """Tests for the inject_context function."""

    def test_returns_none_when_session_manager_is_none(
        self, workflow_state, mock_template_engine
    ) -> None:
        """Should return None and log warning when session_manager is None."""
        result = inject_context(
            session_manager=None,
            session_id="test-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="handoff",
        )
        assert result is None

    def test_returns_none_when_state_is_none(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None and log warning when state is None."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=None,
            template_engine=mock_template_engine,
            source="handoff",
        )
        assert result is None

    def test_returns_none_when_template_engine_is_none(
        self, mock_session_manager, workflow_state
    ) -> None:
        """Should return None and log warning when template_engine is None."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=workflow_state,
            template_engine=None,
            source="handoff",
        )
        assert result is None

    def test_returns_none_when_session_id_is_empty(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when session_id is empty."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id="",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="handoff",
        )
        assert result is None

    def test_returns_none_when_session_id_is_none(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when session_id is None."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id=None,
            state=workflow_state,
            template_engine=mock_template_engine,
            source="handoff",
        )
        assert result is None

    def test_returns_none_when_source_is_none(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when source is None."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            source=None,
        )
        assert result is None

    def test_returns_none_when_source_is_empty(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when source is empty string."""
        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="",
        )
        assert result is None

    def test_previous_session_summary_returns_parent_summary(
        self,
        mock_session_manager,
        workflow_state,
        mock_template_engine,
        mock_session,
        mock_parent_session,
    ) -> None:
        """Should return parent session summary for previous_session_summary source."""
        mock_session.parent_session_id = "parent-session-id"
        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else mock_parent_session
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="previous_session_summary",
        )

        assert result is not None
        assert result["inject_context"] == "Parent session summary content"
        assert workflow_state.context_injected is True

    def test_handoff_source_returns_parent_summary(
        self,
        mock_session_manager,
        workflow_state,
        mock_template_engine,
        mock_session,
        mock_parent_session,
    ) -> None:
        """Should return parent session summary for handoff source."""
        mock_session.parent_session_id = "parent-session-id"
        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else mock_parent_session
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="handoff",
        )

        assert result is not None
        assert result["inject_context"] == "Parent session summary content"

    def test_returns_none_when_session_not_found(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when current session is not found."""
        mock_session_manager.get.return_value = None

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="nonexistent-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="previous_session_summary",
        )

        assert result is None

    def test_returns_none_when_no_parent_session(
        self, mock_session_manager, workflow_state, mock_template_engine, mock_session
    ) -> None:
        """Should return None when current session has no parent."""
        mock_session.parent_session_id = None
        mock_session_manager.get.return_value = mock_session

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="previous_session_summary",
        )

        assert result is None

    def test_returns_none_when_parent_has_no_summary_and_no_failback(
        self, mock_session_manager, workflow_state, mock_template_engine, mock_session
    ) -> None:
        """Should return None when parent session has no summary and no failback file."""
        mock_session.parent_session_id = "parent-session-id"
        parent = MagicMock()
        parent.summary_markdown = None
        parent.external_id = None  # No external_id means no failback lookup

        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else parent
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            source="previous_session_summary",
        )

        assert result is None

    def test_recovers_from_failback_file_when_no_summary(
        self, mock_session_manager, workflow_state, mock_template_engine, mock_session, tmp_path
    ) -> None:
        """Should recover summary from failback file when database summary is empty."""
        from pathlib import Path
        from unittest.mock import patch

        mock_session.parent_session_id = "parent-session-id"
        parent = MagicMock()
        parent.summary_markdown = None
        parent.external_id = "test-external-uuid-123"

        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else parent
        )

        # Create the .gobby directory structure and failback file
        # Path.home() returns tmp_path, so full path is tmp_path / ".gobby" / "session_summaries"
        (tmp_path / ".gobby" / "session_summaries").mkdir(parents=True)
        (
            tmp_path / ".gobby" / "session_summaries" / "session_20240101_test-external-uuid-123.md"
        ).write_text("# Recovered Summary\nRecovered from failback file.")

        # Patch Path.home() to use tmp_path
        with patch.object(Path, "home", return_value=tmp_path):
            result = inject_context(
                session_manager=mock_session_manager,
                session_id="test-session-id",
                state=workflow_state,
                template_engine=mock_template_engine,
                source="previous_session_summary",
            )

        assert result is not None
        assert result["inject_context"] == "# Recovered Summary\nRecovered from failback file."
        assert workflow_state.context_injected is True

    def test_observations_source_with_observations(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should format observations as JSON when source is observations."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {
            "_observations": [{"event": "click"}, {"event": "scroll"}]
        }

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="observations",
            session_variable_manager=mock_svm,
        )

        assert result is not None
        assert "## Observations" in result["inject_context"]
        assert '"event": "click"' in result["inject_context"]

    def test_observations_source_with_empty_observations(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when observations is empty."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {"_observations": []}

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="observations",
            session_variable_manager=mock_svm,
        )

        assert result is None

    def test_workflow_state_source(self, mock_session_manager, mock_template_engine) -> None:
        """Should format workflow state as JSON."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="my-workflow",
            step="planning",
            variables={"count": 5},
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="workflow_state",
        )

        assert result is not None
        assert "## Workflow State" in result["inject_context"]
        assert '"workflow_name": "my-workflow"' in result["inject_context"]
        assert '"step": "planning"' in result["inject_context"]

    def test_workflow_state_with_dict_method_fallback(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should use .dict() method when .model_dump() is not available."""

        # Create a concrete helper class that has dict() but not model_dump
        class TestState:
            def __init__(self):
                self.observations = []
                self.dict_called = False

            def dict(self, exclude=None):
                self.dict_called = True
                return {"workflow_name": "test", "step": "step1"}

        test_state = TestState()
        # Verify no model_dump attribute
        assert not hasattr(test_state, "model_dump")

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=test_state,
            template_engine=mock_template_engine,
            source="workflow_state",
        )

        assert result is not None
        assert "## Workflow State" in result["inject_context"]
        assert test_state.dict_called is True

    def test_compact_handoff_source(
        self, mock_session_manager, mock_template_engine, mock_session
    ) -> None:
        """Should return compact_markdown from current session."""
        mock_session.compact_markdown = "# Compact handoff content"
        mock_session_manager.get.return_value = mock_session

        state = WorkflowState(
            session_id="test-session-id",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=state,
            template_engine=mock_template_engine,
            source="compact_handoff",
        )

        assert result is not None
        assert result["inject_context"] == "# Compact handoff content"

    def test_compact_handoff_with_no_markdown(
        self, mock_session_manager, mock_template_engine, mock_session
    ) -> None:
        """Should return None when compact_markdown is not set."""
        mock_session.compact_markdown = None
        mock_session_manager.get.return_value = mock_session

        state = WorkflowState(
            session_id="test-session-id",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=state,
            template_engine=mock_template_engine,
            source="compact_handoff",
        )

        assert result is None

    def test_with_template_rendering_for_handoff(
        self, mock_session_manager, workflow_state, mock_session, mock_parent_session
    ) -> None:
        """Should render template with context for handoff source."""
        mock_session.parent_session_id = "parent-session-id"
        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else mock_parent_session
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered content"

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=template_engine,
            source="handoff",
            template="## Context\n{{ summary }}",
        )

        assert result is not None
        assert result["inject_context"] == "Rendered content"
        template_engine.render.assert_called_once()
        call_args = template_engine.render.call_args
        assert call_args[0][0] == "## Context\n{{ summary }}"
        assert "summary" in call_args[0][1]
        assert "handoff" in call_args[0][1]

    def test_with_template_rendering_for_observations(self, mock_session_manager) -> None:
        """Should render template with observations_text for observations source."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered observations"

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {"_observations": [{"event": "click"}]}

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=template_engine,
            source="observations",
            template="Obs: {{ observations_text }}",
            session_variable_manager=mock_svm,
        )

        assert result is not None
        call_args = template_engine.render.call_args
        assert "observations_text" in call_args[0][1]

    def test_with_template_rendering_for_workflow_state(self, mock_session_manager) -> None:
        """Should render template with workflow_state_text for workflow_state source."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered state"

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=template_engine,
            source="workflow_state",
            template="State: {{ workflow_state_text }}",
        )

        assert result is not None
        call_args = template_engine.render.call_args
        assert "workflow_state_text" in call_args[0][1]

    def test_with_template_rendering_for_compact_handoff(
        self, mock_session_manager, mock_session
    ) -> None:
        """Should render template with handoff for compact_handoff source."""
        mock_session.compact_markdown = "Compact content"
        mock_session_manager.get.return_value = mock_session
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered compact"

        state = WorkflowState(
            session_id="test-session-id",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=state,
            template_engine=template_engine,
            source="compact_handoff",
            template="Handoff: {{ handoff }}",
        )

        assert result is not None
        call_args = template_engine.render.call_args
        assert "handoff" in call_args[0][1]
        assert call_args[0][1]["handoff"] == "Compact content"

    def test_template_rendering_with_previous_session_summary_source(
        self, mock_session_manager, mock_session, mock_parent_session
    ) -> None:
        """Should set summary and handoff in render context for previous_session_summary."""
        mock_session.parent_session_id = "parent-id"
        mock_session_manager.get.side_effect = lambda sid: (
            mock_session if sid == "test-session-id" else mock_parent_session
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered with summary"

        state = WorkflowState(
            session_id="test-session-id",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=state,
            template_engine=template_engine,
            source="previous_session_summary",
            template="Template: {{ summary }} - {{ handoff.notes }}",
        )

        assert result is not None
        call_args = template_engine.render.call_args[0][1]
        assert call_args["summary"] == "Parent session summary content"
        assert call_args["handoff"]["notes"] == "Parent session summary content"

    def test_require_blocks_when_no_content(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return block decision when require=True and no content found."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="unknown_source",
            require=True,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Required handoff context not found" in result["reason"]

    def test_require_false_returns_none_when_no_content(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when require=False and no content found."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="unknown_source",
            require=False,
        )

        assert result is None

    def test_unknown_source_returns_none(self, mock_session_manager, mock_template_engine) -> None:
        """Should return None for unknown source type."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="unknown_source",
        )

        assert result is None


# --- Tests for inject_message ---


class TestInjectMessage:
    """Tests for the inject_message function."""

    def test_returns_none_when_content_is_none(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when content is None."""
        result = inject_message(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            content=None,
        )
        assert result is None

    def test_returns_none_when_content_is_empty(
        self, mock_session_manager, workflow_state, mock_template_engine
    ) -> None:
        """Should return None when content is empty string."""
        result = inject_message(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=workflow_state,
            template_engine=mock_template_engine,
            content="",
        )
        assert result is None

    def test_renders_and_returns_message(
        self, mock_session_manager, workflow_state, mock_session
    ) -> None:
        """Should render template and return inject_message dict."""
        mock_session_manager.get.return_value = mock_session
        template_engine = MagicMock()
        template_engine.render.return_value = "Hello, world!"

        result = inject_message(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=template_engine,
            content="Hello, {{ name }}!",
        )

        assert result is not None
        assert result["inject_message"] == "Hello, world!"
        template_engine.render.assert_called_once()

    def test_includes_state_in_render_context(
        self, mock_session_manager, workflow_state, mock_template_engine, mock_session
    ) -> None:
        """Should include state data in template render context."""
        mock_session_manager.get.return_value = mock_session
        mock_template_engine.render.return_value = "rendered"

        inject_message(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            content="template",
        )

        call_args = mock_template_engine.render.call_args[0][1]
        assert "session" in call_args
        assert "state" in call_args
        assert "step_action_count" in call_args
        assert "variables" in call_args

    def test_includes_extra_kwargs_in_render_context(
        self, mock_session_manager, workflow_state, mock_template_engine, mock_session
    ) -> None:
        """Should include extra kwargs in template render context."""
        mock_session_manager.get.return_value = mock_session
        mock_template_engine.render.return_value = "rendered"

        inject_message(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=workflow_state,
            template_engine=mock_template_engine,
            content="template",
            custom_var="custom_value",
            another_var=123,
        )

        call_args = mock_template_engine.render.call_args[0][1]
        assert call_args["custom_var"] == "custom_value"
        assert call_args["another_var"] == 123

    def test_handles_none_variables_in_state(
        self, mock_session_manager, mock_template_engine, mock_session
    ) -> None:
        """Should handle None variables in state gracefully."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        # Manually set variables to None to test edge case
        state.variables = None

        mock_session_manager.get.return_value = mock_session
        mock_template_engine.render.return_value = "rendered"

        result = inject_message(
            session_manager=mock_session_manager,
            session_id="test-session-id",
            state=state,
            template_engine=mock_template_engine,
            content="template",
        )

        assert result is not None
        call_args = mock_template_engine.render.call_args[0][1]
        assert call_args["variables"] == {}


# --- Tests for format_handoff_as_markdown ---


class TestFormatHandoffAsMarkdown:
    """Tests for the format_handoff_as_markdown function."""

    @dataclass
    class MockHandoffContext:
        """Mock HandoffContext for testing."""

        active_gobby_task: dict | None = None
        active_worktree: dict | None = None
        git_commits: list = field(default_factory=list)
        git_status: str = ""
        files_modified: list = field(default_factory=list)
        initial_goal: str = ""
        recent_activity: list = field(default_factory=list)

    def test_empty_context_returns_empty_string(self) -> None:
        """Should return empty string when all context fields are empty."""
        ctx = self.MockHandoffContext()
        result = format_handoff_as_markdown(ctx)
        assert result == ""

    def test_formats_active_task(self) -> None:
        """Should format active task section."""
        ctx = self.MockHandoffContext(
            active_gobby_task={"id": "gt-123", "title": "Fix auth bug", "status": "in_progress"}
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Active Task" in result
        assert "**Fix auth bug** (gt-123)" in result
        assert "Status: in_progress" in result

    def test_formats_active_task_with_missing_fields(self) -> None:
        """Should handle missing fields in active task with defaults."""
        ctx = self.MockHandoffContext(
            active_gobby_task={"some_field": "value"}  # Non-empty dict with no title/id/status
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Active Task" in result
        assert "**Untitled** (unknown)" in result
        assert "Status: unknown" in result

    def test_formats_worktree_context(self) -> None:
        """Should format worktree context section."""
        ctx = self.MockHandoffContext(
            active_worktree={
                "branch_name": "feature/auth",
                "worktree_path": "/path/to/worktree",
                "base_branch": "main",
                "task_id": "gt-123",
            }
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Worktree Context" in result
        assert "**Branch**: `feature/auth`" in result
        assert "**Path**: `/path/to/worktree`" in result
        assert "**Base**: `main`" in result
        assert "**Task**: gt-123" in result

    def test_formats_worktree_without_task_id(self) -> None:
        """Should format worktree without task_id."""
        ctx = self.MockHandoffContext(
            active_worktree={
                "branch_name": "feature/auth",
                "worktree_path": "/path",
                "base_branch": "main",
            }
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Worktree Context" in result
        assert "**Task**" not in result

    def test_formats_git_commits(self) -> None:
        """Should format git commits section."""
        ctx = self.MockHandoffContext(
            git_commits=[
                {"hash": "abc123def456", "message": "feat: add feature"},
                {"hash": "789xyz", "message": "fix: bug fix"},
            ]
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Commits This Session" in result
        assert "- `abc123d` feat: add feature" in result
        assert "- `789xyz` fix: bug fix" in result

    def test_formats_git_status(self) -> None:
        """Should format git status section."""
        ctx = self.MockHandoffContext(git_status="M src/file.py\nA new_file.py")
        result = format_handoff_as_markdown(ctx)

        assert "### Uncommitted Changes" in result
        assert "```\nM src/file.py\nA new_file.py\n```" in result

    def test_formats_files_modified(self) -> None:
        """Should format files modified section only for uncommitted files."""
        # Files only shown if they appear in git_status (still uncommitted)
        ctx = self.MockHandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="M src/auth.py\nM tests/test_auth.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" in result
        assert "- src/auth.py" in result
        assert "- tests/test_auth.py" in result

    def test_files_modified_filters_committed_files(self) -> None:
        """Should not show files that are no longer in git status (committed)."""
        # auth.py was committed, test_auth.py still dirty
        ctx = self.MockHandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="M tests/test_auth.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" in result
        assert "- src/auth.py" not in result  # Committed, not in git_status
        assert "- tests/test_auth.py" in result

    def test_files_modified_not_shown_without_git_status(self) -> None:
        """Should not show files modified section if git_status is empty."""
        ctx = self.MockHandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="",  # No git status means can't verify dirty files
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" not in result

    def test_formats_initial_goal(self) -> None:
        """Should format initial goal section when no task or task is active."""
        ctx = self.MockHandoffContext(initial_goal="Implement user authentication")
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result
        assert "Implement user authentication" in result

    def test_initial_goal_shown_for_open_task(self) -> None:
        """Should show initial goal when task status is open."""
        ctx = self.MockHandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={"id": "gt-123", "title": "Fix bug", "status": "open"},
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result

    def test_initial_goal_shown_for_in_progress_task(self) -> None:
        """Should show initial goal when task status is in_progress."""
        ctx = self.MockHandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={"id": "gt-123", "title": "Fix bug", "status": "in_progress"},
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result

    def test_initial_goal_hidden_for_closed_task(self) -> None:
        """Should not show initial goal when task is closed."""
        ctx = self.MockHandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={"id": "gt-123", "title": "Fix bug", "status": "closed"},
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result

    def test_initial_goal_hidden_for_completed_task(self) -> None:
        """Should not show initial goal when task is completed."""
        ctx = self.MockHandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={"id": "gt-123", "title": "Fix bug", "status": "completed"},
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result

    def test_formats_recent_activity(self) -> None:
        """Should format recent activity section with max 5 items."""
        ctx = self.MockHandoffContext(
            recent_activity=[
                "Activity 1",
                "Activity 2",
                "Activity 3",
                "Activity 4",
                "Activity 5",
                "Activity 6",
                "Activity 7",
            ]
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Recent Activity" in result
        # Should only include last 5
        assert "- Activity 3" in result
        assert "- Activity 4" in result
        assert "- Activity 5" in result
        assert "- Activity 6" in result
        assert "- Activity 7" in result
        assert "- Activity 1" not in result
        assert "- Activity 2" not in result

    def test_formats_multiple_sections(self) -> None:
        """Should format multiple sections separated by double newlines."""
        ctx = self.MockHandoffContext(
            initial_goal="Fix the bug",
            git_status="M file.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "\n\n" in result
        sections = result.split("\n\n")
        assert len(sections) == 2

    def test_prompt_template_parameter_is_ignored(self) -> None:
        """Should ignore prompt_template parameter (reserved for future)."""
        ctx = self.MockHandoffContext(initial_goal="Goal")
        result = format_handoff_as_markdown(ctx, prompt_template="custom template")

        assert "### Original Goal" in result
        assert "Goal" in result

    def test_handles_empty_strings_in_context(self) -> None:
        """Should not include sections with empty strings."""
        ctx = self.MockHandoffContext(
            initial_goal="",
            git_status="",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result
        assert "### Uncommitted Changes" not in result

    def test_handles_commit_with_empty_hash(self) -> None:
        """Should handle commits with empty hash gracefully."""
        ctx = self.MockHandoffContext(git_commits=[{"hash": "", "message": "test commit"}])
        result = format_handoff_as_markdown(ctx)

        assert "### Commits This Session" in result
        assert "- `` test commit" in result

    def test_active_skills_section_removed(self) -> None:
        """Active skills section was removed - redundant with _build_skill_injection_context()."""
        # Skills are now only injected via _build_skill_injection_context() on session start
        ctx = self.MockHandoffContext(git_commits=[{"hash": "abc1234", "message": "test"}])
        result = format_handoff_as_markdown(ctx)

        assert "### Active Skills" not in result
        assert "Skills available:" not in result


# --- Tests for recommend_skills_for_task ---


class TestRecommendSkillsForTask:
    """Tests for the recommend_skills_for_task function."""

    def test_import(self) -> None:
        """Test that recommend_skills_for_task can be imported."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        assert recommend_skills_for_task is not None

    def test_returns_list(self) -> None:
        """Should return a list of skill names."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        result = recommend_skills_for_task({"title": "Test task"})
        assert isinstance(result, list)

    def test_with_code_category(self) -> None:
        """Should return code-related skills for code category."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        task = {"title": "Test task", "category": "code"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result

    def test_with_docs_category(self) -> None:
        """Should return docs-related skills for docs category."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        task = {"title": "Test task", "category": "docs"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result
        assert "gobby-plan" in result

    def test_with_test_category(self) -> None:
        """Should return test-related skills for test category."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        task = {"title": "Test task", "category": "test"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result

    def test_with_no_category(self) -> None:
        """Should return always-apply skills when no category."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        task = {"title": "Test task"}
        result = recommend_skills_for_task(task)

        assert isinstance(result, list)

    def test_with_none_task(self) -> None:
        """Should return empty list for None task."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        result = recommend_skills_for_task(None)
        assert result == []

    def test_with_empty_dict(self) -> None:
        """Should return always-apply skills for empty dict."""
        from gobby.workflows.context_actions import recommend_skills_for_task

        result = recommend_skills_for_task({})
        assert isinstance(result, list)


# --- Tests for inject_context with source='skills' ---


class TestInjectContextSkillsSource:
    """Tests for inject_context with source='skills'.

    Part of epic #6640: Consolidate Skill Injection into Workflows.
    """

    @pytest.fixture
    def mock_skill_manager(self):
        """Create a mock skill manager with test skills."""
        manager = MagicMock()

        # Create mock ParsedSkill objects
        always_apply_skill = MagicMock()
        always_apply_skill.name = "proactive-memory"
        always_apply_skill.description = "Quick reference for claiming tasks"
        always_apply_skill.is_always_apply.return_value = True

        regular_skill = MagicMock()
        regular_skill.name = "gobby-tasks"
        regular_skill.description = "Task management skill"
        regular_skill.is_always_apply.return_value = False

        another_skill = MagicMock()
        another_skill.name = "gobby-sessions"
        another_skill.description = "Session management skill"
        another_skill.is_always_apply.return_value = False

        manager.discover_core_skills.return_value = [
            always_apply_skill,
            regular_skill,
            another_skill,
        ]

        return manager

    def test_skills_source_returns_formatted_skill_list(
        self, mock_session_manager, mock_template_engine, mock_skill_manager
    ) -> None:
        """Should return formatted skill list when source is 'skills'."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=mock_skill_manager,
        )

        assert result is not None
        assert "inject_context" in result
        content = result["inject_context"]
        # Should contain skill names
        assert "proactive-memory" in content
        assert "gobby-tasks" in content
        assert "gobby-sessions" in content

    def test_skills_source_filters_always_apply_skills(
        self, mock_session_manager, mock_template_engine, mock_skill_manager
    ) -> None:
        """Should only include always_apply skills when filter='always_apply'."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=mock_skill_manager,
            filter="always_apply",
        )

        assert result is not None
        content = result["inject_context"]
        # Should contain always_apply skill
        assert "proactive-memory" in content
        # Should NOT contain non-always_apply skills
        assert "gobby-tasks" not in content
        assert "gobby-sessions" not in content

    def test_skills_source_returns_none_without_skill_manager(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when skill_manager is not provided."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=None,
        )

        assert result is None

    def test_skills_source_returns_none_when_no_skills_discovered(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when skill manager returns empty list."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.discover_core_skills.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=empty_manager,
        )

        assert result is None

    def test_skills_source_with_template_rendering(
        self, mock_session_manager, mock_skill_manager
    ) -> None:
        """Should render template with skills_list for skills source."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered skills"

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=template_engine,
            source="skills",
            template="Skills: {{ skills_list }}",
            skill_manager=mock_skill_manager,
        )

        assert result is not None
        assert result["inject_context"] == "Rendered skills"
        call_args = template_engine.render.call_args
        assert "skills_list" in call_args[0][1]

    def test_skills_source_sets_context_injected_flag(
        self, mock_session_manager, mock_template_engine, mock_skill_manager
    ) -> None:
        """Should set context_injected flag on state when skills injected."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        assert state.context_injected is False

        inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=mock_skill_manager,
        )

        assert state.context_injected is True

    def test_skills_source_require_blocks_when_no_skills(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return block decision when require=True and no skills found."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.discover_core_skills.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",
            skill_manager=empty_manager,
            require=True,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Required handoff context not found" in result["reason"]


# --- Tests for inject_context with source='task_context' ---


class TestInjectContextTaskContextSource:
    """Tests for inject_context with source='task_context'.

    Part of epic #6640: Consolidate Skill Injection into Workflows.
    """

    @pytest.fixture
    def mock_session_task_manager(self):
        """Create a mock session task manager."""
        manager = MagicMock()

        # Create mock task object
        mock_task = MagicMock()
        mock_task.id = "task-uuid-123"
        mock_task.seq_num = 6644
        mock_task.title = "Implement user authentication"
        mock_task.status = "in_progress"
        mock_task.description = "Add login/logout functionality"
        mock_task.validation_criteria = "Tests pass and login works"

        # Return task in the worked_on action format
        manager.get_session_tasks.return_value = [
            {
                "task": mock_task,
                "action": "worked_on",
                "link_created_at": "2026-01-15T10:00:00Z",
            }
        ]

        return manager

    def test_task_context_source_returns_formatted_task_info(
        self, mock_session_manager, mock_template_engine, mock_session_task_manager
    ) -> None:
        """Should return formatted task info when source is 'task_context'."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=mock_session_task_manager,
        )

        assert result is not None
        assert "inject_context" in result
        content = result["inject_context"]
        # Should contain task details
        assert "#6644" in content or "6644" in content
        assert "Implement user authentication" in content
        assert "in_progress" in content

    def test_task_context_source_returns_none_without_session_task_manager(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when session_task_manager is not provided."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=None,
        )

        assert result is None

    def test_task_context_source_returns_none_when_no_active_task(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when session has no tasks."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.get_session_tasks.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=empty_manager,
        )

        assert result is None

    def test_task_context_source_filters_worked_on_tasks(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should only include tasks with 'worked_on' action."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        # Create mock tasks with different actions
        worked_on_task = MagicMock()
        worked_on_task.id = "task-1"
        worked_on_task.seq_num = 100
        worked_on_task.title = "Active task"
        worked_on_task.status = "in_progress"
        worked_on_task.description = "The active task"
        worked_on_task.validation_criteria = None

        mentioned_task = MagicMock()
        mentioned_task.id = "task-2"
        mentioned_task.seq_num = 200
        mentioned_task.title = "Mentioned task"
        mentioned_task.status = "open"

        manager = MagicMock()
        manager.get_session_tasks.return_value = [
            {
                "task": worked_on_task,
                "action": "worked_on",
                "link_created_at": "2026-01-15T10:00:00Z",
            },
            {
                "task": mentioned_task,
                "action": "mentioned",
                "link_created_at": "2026-01-15T09:00:00Z",
            },
        ]

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=manager,
        )

        assert result is not None
        content = result["inject_context"]
        # Should contain worked_on task
        assert "Active task" in content
        # Should NOT contain mentioned task
        assert "Mentioned task" not in content

    def test_task_context_source_with_template_rendering(
        self, mock_session_manager, mock_session_task_manager
    ) -> None:
        """Should render template with task_context for task_context source."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered task context"

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=template_engine,
            source="task_context",
            template="Task: {{ task_context }}",
            session_task_manager=mock_session_task_manager,
        )

        assert result is not None
        assert result["inject_context"] == "Rendered task context"
        call_args = template_engine.render.call_args
        assert "task_context" in call_args[0][1]

    def test_task_context_source_sets_context_injected_flag(
        self, mock_session_manager, mock_template_engine, mock_session_task_manager
    ) -> None:
        """Should set context_injected flag on state when task context injected."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        assert state.context_injected is False

        inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=mock_session_task_manager,
        )

        assert state.context_injected is True

    def test_task_context_source_require_blocks_when_no_task(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return block decision when require=True and no task found."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.get_session_tasks.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="task_context",
            session_task_manager=empty_manager,
            require=True,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Required handoff context not found" in result["reason"]


# --- Tests for inject_context with source='memories' ---


class TestInjectContextMemoriesSource:
    """Tests for inject_context with source='memories'.

    Part of epic #6640: Consolidate Skill Injection into Workflows.
    """

    @pytest.fixture
    def mock_memory_manager(self):
        """Create a mock memory manager."""
        manager = MagicMock()
        # Enable config
        manager.config.enabled = True

        # Create mock memory objects
        memory1 = MagicMock()
        memory1.id = "mem-1"
        memory1.content = "User prefers dark mode for all UIs"
        memory1.memory_type = "preference"

        memory2 = MagicMock()
        memory2.id = "mem-2"
        memory2.content = "Project uses pytest for testing"
        memory2.memory_type = "fact"

        manager.search_memories.return_value = [memory1, memory2]

        return manager

    def test_memories_source_returns_formatted_memories(
        self, mock_session_manager, mock_template_engine, mock_memory_manager, mock_session
    ) -> None:
        """Should return formatted memories when source is 'memories'."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=mock_memory_manager,
            prompt_text="What UI theme should I use?",
        )

        assert result is not None
        assert "inject_context" in result
        content = result["inject_context"]
        # Should contain memory content
        assert "dark mode" in content
        assert "pytest" in content

    def test_memories_source_returns_none_without_memory_manager(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when memory_manager is not provided."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=None,
            prompt_text="test prompt",
        )

        assert result is None

    def test_memories_source_returns_none_when_disabled(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when memory manager is disabled."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        disabled_manager = MagicMock()
        disabled_manager.config.enabled = False

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=disabled_manager,
            prompt_text="test prompt",
        )

        assert result is None

    def test_memories_source_returns_none_when_no_memories_found(
        self, mock_session_manager, mock_template_engine, mock_session
    ) -> None:
        """Should return None when no memories are found."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.config.enabled = True
        empty_manager.search_memories.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=empty_manager,
            prompt_text="find something",
        )

        assert result is None

    def test_memories_source_uses_limit_parameter(
        self, mock_session_manager, mock_template_engine, mock_memory_manager, mock_session
    ) -> None:
        """Should pass limit parameter to memory recall."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=mock_memory_manager,
            prompt_text="test prompt",
            limit=10,
        )

        # Verify limit was passed to recall
        mock_memory_manager.search_memories.assert_called_once()
        call_kwargs = mock_memory_manager.search_memories.call_args[1]
        assert call_kwargs["limit"] == 10

    def test_memories_source_with_template_rendering(
        self, mock_session_manager, mock_memory_manager, mock_session
    ) -> None:
        """Should render template with memories_list for memories source."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        template_engine = MagicMock()
        template_engine.render.return_value = "Rendered memories"

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=template_engine,
            source="memories",
            template="Memories: {{ memories_list }}",
            memory_manager=mock_memory_manager,
            prompt_text="test prompt",
        )

        assert result is not None
        assert result["inject_context"] == "Rendered memories"
        call_args = template_engine.render.call_args
        assert "memories_list" in call_args[0][1]

    def test_memories_source_sets_context_injected_flag(
        self, mock_session_manager, mock_template_engine, mock_memory_manager, mock_session
    ) -> None:
        """Should set context_injected flag on state when memories injected."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        assert state.context_injected is False

        inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=mock_memory_manager,
            prompt_text="test prompt",
        )

        assert state.context_injected is True

    def test_memories_source_require_blocks_when_no_memories(
        self, mock_session_manager, mock_template_engine, mock_session
    ) -> None:
        """Should return block decision when require=True and no memories found."""
        mock_session_manager.get.return_value = mock_session
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        empty_manager = MagicMock()
        empty_manager.config.enabled = True
        empty_manager.search_memories.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="memories",
            memory_manager=empty_manager,
            prompt_text="find something",
            require=True,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Required handoff context not found" in result["reason"]


# --- Tests for inject_context with array source syntax ---


class TestInjectContextArraySource:
    """Tests for inject_context with source as list.

    Part of epic #6640: Consolidate Skill Injection into Workflows.
    """

    @pytest.fixture
    def mock_skill_manager(self):
        """Create a mock skill manager with test skills."""
        manager = MagicMock()
        skill = MagicMock()
        skill.name = "gobby-tasks"
        skill.description = "Task management skill"
        skill.is_always_apply.return_value = False
        manager.discover_core_skills.return_value = [skill]
        return manager

    @pytest.fixture
    def mock_session_task_manager(self):
        """Create a mock session task manager."""
        manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-uuid-123"
        mock_task.seq_num = 100
        mock_task.title = "Test task"
        mock_task.status = "in_progress"
        mock_task.description = "Test description"
        mock_task.validation_criteria = None
        manager.get_session_tasks.return_value = [
            {"task": mock_task, "action": "worked_on", "link_created_at": "2026-01-15T10:00:00Z"}
        ]
        return manager

    def test_array_source_combines_multiple_sources(
        self,
        mock_session_manager,
        mock_template_engine,
        mock_skill_manager,
        mock_session_task_manager,
    ) -> None:
        """Should combine content from multiple sources when source is a list."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=mock_skill_manager,
            session_task_manager=mock_session_task_manager,
        )

        assert result is not None
        assert "inject_context" in result
        content = result["inject_context"]
        # Should contain content from both sources
        assert "gobby-tasks" in content  # From skills
        assert "Test task" in content  # From task_context

    def test_array_source_skips_empty_sources(
        self, mock_session_manager, mock_template_engine, mock_skill_manager
    ) -> None:
        """Should skip sources that return no content."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        # session_task_manager returns empty
        empty_task_manager = MagicMock()
        empty_task_manager.get_session_tasks.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=mock_skill_manager,
            session_task_manager=empty_task_manager,
        )

        assert result is not None
        content = result["inject_context"]
        # Should contain skills content but not task_context
        assert "gobby-tasks" in content

    def test_array_source_returns_none_when_all_sources_empty(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should return None when all sources in list return no content."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        # Empty skill manager
        empty_skill_manager = MagicMock()
        empty_skill_manager.discover_core_skills.return_value = []

        # Empty task manager
        empty_task_manager = MagicMock()
        empty_task_manager.get_session_tasks.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=empty_skill_manager,
            session_task_manager=empty_task_manager,
        )

        assert result is None

    def test_array_source_separates_content_with_newlines(
        self,
        mock_session_manager,
        mock_template_engine,
        mock_skill_manager,
        mock_session_task_manager,
    ) -> None:
        """Should separate content from different sources with newlines."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=mock_skill_manager,
            session_task_manager=mock_session_task_manager,
        )

        assert result is not None
        content = result["inject_context"]
        # Content from different sources should be separated
        assert "\n\n" in content

    def test_array_source_sets_context_injected_flag(
        self,
        mock_session_manager,
        mock_template_engine,
        mock_skill_manager,
        mock_session_task_manager,
    ) -> None:
        """Should set context_injected flag when any source provides content."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )
        assert state.context_injected is False

        inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=mock_skill_manager,
            session_task_manager=mock_session_task_manager,
        )

        assert state.context_injected is True

    def test_single_string_source_still_works(
        self, mock_session_manager, mock_template_engine, mock_skill_manager
    ) -> None:
        """Should still work with single string source (backward compatibility)."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source="skills",  # Single string, not list
            skill_manager=mock_skill_manager,
        )

        assert result is not None
        assert "gobby-tasks" in result["inject_context"]

    def test_array_source_require_blocks_when_all_empty(
        self, mock_session_manager, mock_template_engine
    ) -> None:
        """Should block when require=True and all sources are empty."""
        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        # Empty managers
        empty_skill_manager = MagicMock()
        empty_skill_manager.discover_core_skills.return_value = []
        empty_task_manager = MagicMock()
        empty_task_manager.get_session_tasks.return_value = []

        result = inject_context(
            session_manager=mock_session_manager,
            session_id="test-session",
            state=state,
            template_engine=mock_template_engine,
            source=["skills", "task_context"],
            skill_manager=empty_skill_manager,
            session_task_manager=empty_task_manager,
            require=True,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Required handoff context not found" in result["reason"]
