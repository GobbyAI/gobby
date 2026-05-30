"""Focused tests for session storage behavior."""

import pytest

from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSession:
    """Tests for Session dataclass."""

    def test_from_row(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test creating Session from database row."""
        session = session_manager.register(
            external_id="test-cli-key",
            machine_id="test-machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update(session.id, title_source="manual")
        session_manager.update_context_usage(
            session.id,
            ContextUsageSnapshot.from_token_breakdown(
                source="codex",
                context_window=100_000,
                uncached_prompt_tokens=1_000,
                cache_read_tokens=2_000,
                cache_creation_tokens=300,
                output_tokens=50,
                model="gpt-5-codex",
            ),
        )

        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = %s", (session.id,))
        assert row is not None

        session_from_row = Session.from_row(row)
        assert session_from_row.id == session.id
        assert session_from_row.external_id == "test-cli-key"
        assert session_from_row.source == "claude"
        assert session_from_row.title_source == "manual"
        assert session_from_row.context_used_tokens == 3300
        assert session_from_row.last_prompt_uncached_input_tokens == 1000
        assert session_from_row.last_prompt_cache_read_tokens == 2000
        assert session_from_row.last_prompt_cache_creation_tokens == 300
        assert session_from_row.last_completion_output_tokens == 50
        assert session_from_row.to_dict()["context_usage_source"] == "codex"

    def test_to_dict(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test converting Session to dictionary."""
        session = session_manager.register(
            external_id="dict-test",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
            title="Test Session",
        )

        d = session.to_dict()
        assert d["id"] == session.id
        assert d["external_id"] == "dict-test"
        assert d["machine_id"] == "machine-1"
        assert d["source"] == "gemini"
        assert d["title"] == "Test Session"
        assert d["title_source"] is None
        assert d["status"] == "active"

    def test_to_dict_and_brief_include_title_source(self) -> None:
        session = Session(
            id="sess-title-source",
            external_id="ext-title-source",
            machine_id="machine-1",
            source="gemini",
            project_id="proj-1",
            title="Titled Session",
            title_source="manual",
            status="active",
            transcript_path=None,
            summary_path=None,
            summary_markdown=None,
            git_branch=None,
            parent_session_id=None,
            created_at="2026-04-16T00:00:00Z",
            updated_at="2026-04-16T00:05:00Z",
        )

        assert session.to_dict()["title_source"] == "manual"
        assert session.to_brief()["title_source"] == "manual"

    def test_to_dict_marks_live_tmux_sessions_proxy_attachable(self) -> None:
        """Paused tmux sessions remain attachable while terminal liveness metadata exists."""
        session = Session(
            id="sess-live-tmux",
            external_id="ext-live-tmux",
            machine_id="machine-1",
            source="qwen",
            project_id="proj-1",
            title="Live tmux session",
            status="paused",
            transcript_path=None,
            summary_path=None,
            summary_markdown=None,
            git_branch="main",
            parent_session_id=None,
            created_at="2026-04-16T00:00:00Z",
            updated_at="2026-04-16T00:05:00Z",
            terminal_context={"tmux_pane": "%12"},
            session_type="terminal",
        )

        assert session.can_proxy_attach is True
        assert session.to_dict()["can_proxy_attach"] is True

    def test_parent_pid_does_not_count_as_terminal_liveness(self) -> None:
        session = Session(
            id="sess-parent-pid-only",
            external_id="ext-parent-pid-only",
            machine_id="machine-1",
            source="qwen",
            project_id="proj-1",
            title="Stale pid session",
            status="paused",
            transcript_path=None,
            summary_path=None,
            summary_markdown=None,
            git_branch="main",
            parent_session_id=None,
            created_at="2026-04-16T00:00:00Z",
            updated_at="2026-04-16T00:05:00Z",
            terminal_context={"parent_pid": 12345},
            session_type="terminal",
        )

        assert session.has_terminal_liveness is False
        assert session.can_proxy_attach is False


class TestSessionManagerModelFields:
    """Session serialization coverage from SessionManager tests."""

    def test_session_to_dict_includes_all_fields(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that to_dict includes all session fields."""
        session = session_manager.register(
            external_id="dict-complete",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
            title="Test",
            transcript_path="/path.jsonl",
            git_branch="main",
            parent_session_id=None,
            agent_depth=1,
            spawned_by_agent_id=None,  # Not a FK, but no need to test with value
        )

        # Update terminal pickup metadata (without agent_run_id to avoid FK constraint)
        session_manager.update_terminal_pickup_metadata(
            session.id,
            workflow_name="plan-execute",
            context_injected=True,
            original_prompt="Test prompt",
        )

        # Update other fields
        session_manager.update_summary(session.id, "/summary.md", "# Summary")

        # Retrieve and convert to dict
        full_session = session_manager.get(session.id)
        d = full_session.to_dict()

        assert "id" in d
        assert "external_id" in d
        assert "machine_id" in d
        assert "source" in d
        assert "project_id" in d
        assert "title" in d
        assert "status" in d
        assert "transcript_path" in d
        assert "summary_path" in d
        assert "summary_markdown" in d
        assert "git_branch" in d
        assert "parent_session_id" in d
        assert "agent_depth" in d
        assert "spawned_by_agent_id" in d
        assert "workflow_name" in d
        assert "agent_run_id" in d
        assert "context_injected" in d
        assert "original_prompt" in d
        assert "created_at" in d
        assert "updated_at" in d
