"""Tests for the LocalSessionManager storage layer."""

from unittest.mock import patch

import pytest

from gobby.storage.session_models import Session
from gobby.storage.sessions import SYSTEM_SESSION_ID, LocalSessionManager

pytestmark = pytest.mark.unit


class TestSession:
    """Tests for Session dataclass."""

    def test_from_row(
        self,
        session_manager: LocalSessionManager,
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

        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session.id,))
        assert row is not None

        session_from_row = Session.from_row(row)
        assert session_from_row.id == session.id
        assert session_from_row.external_id == "test-cli-key"
        assert session_from_row.source == "claude"
        assert session_from_row.title_source == "manual"

    def test_to_dict(
        self,
        session_manager: LocalSessionManager,
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


class TestLocalSessionManager:
    """Tests for LocalSessionManager class."""

    def test_register_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test registering a new session."""
        session = session_manager.register(
            external_id="session-123",
            machine_id="machine-abc",
            source="claude",
            project_id=sample_project["id"],
            title="My Session",
            transcript_path="/path/to/transcript.jsonl",
            git_branch="main",
        )

        assert session.id is not None
        assert session.external_id == "session-123"
        assert session.machine_id == "machine-abc"
        assert session.source == "claude"
        assert session.project_id == sample_project["id"]
        assert session.title == "My Session"
        assert session.status == "active"
        assert session.transcript_path == "/path/to/transcript.jsonl"
        assert session.git_branch == "main"

        # Verify stats columns
        assert session.message_count == 0
        assert session.turn_count == 0
        assert session.tool_call_count == 0
        assert session.last_assistant_content is None

    def test_register_recreates_missing_system_parent_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Register self-heals the system parent row before inserting children."""
        session_manager.db.execute("DELETE FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
        assert (
            session_manager.db.fetchone(
                "SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,)
            )
            is None
        )

        session = session_manager.register(
            external_id="pipeline-child",
            machine_id="machine-abc",
            source="pipeline",
            project_id=sample_project["id"],
            parent_session_id=SYSTEM_SESSION_ID,
        )

        repaired = session_manager.db.fetchone(
            "SELECT id, external_id, source FROM sessions WHERE id = ?",
            (SYSTEM_SESSION_ID,),
        )
        assert repaired is not None
        assert repaired["external_id"] == "system"
        assert repaired["source"] == "system"
        assert session.parent_session_id == SYSTEM_SESSION_ID

    def test_register_session_has_stats_columns(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that a newly registered session has the stats columns."""
        session = session_manager.register(
            external_id="stats-check",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Verify Session object has fields
        assert hasattr(session, "message_count")
        assert hasattr(session, "turn_count")
        assert hasattr(session, "tool_call_count")
        assert hasattr(session, "last_assistant_content")

        # Verify values from DB
        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session.id,))
        assert "message_count" in row.keys()
        assert "turn_count" in row.keys()
        assert "tool_call_count" in row.keys()
        assert "last_assistant_content" in row.keys()

        assert row["message_count"] == 0
        assert row["turn_count"] == 0
        assert row["tool_call_count"] == 0
        assert row["last_assistant_content"] is None

    def test_register_persists_sandbox_metadata(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="sandboxed-session",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            sandbox_enabled=True,
            sandbox_policy_hash="policy-abc",
        )

        assert session.sandbox_enabled is True
        assert session.sandbox_policy_hash == "policy-abc"

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.sandbox_enabled is True
        assert reloaded.sandbox_policy_hash == "policy-abc"

    def test_register_preserves_unknown_sandbox_metadata_as_null(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="unknown-sandbox-session",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            sandbox_enabled=None,
            sandbox_policy_hash=None,
        )

        row = session_manager.db.fetchone(
            "SELECT sandbox_enabled, sandbox_policy_hash FROM sessions WHERE id = ?",
            (session.id,),
        )
        assert row is not None
        assert row["sandbox_enabled"] is None
        assert row["sandbox_policy_hash"] is None

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.sandbox_enabled is None
        assert reloaded.sandbox_policy_hash is None

    def test_register_upserts_on_conflict(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register updates existing session on conflict."""
        # First registration
        session1 = session_manager.register(
            external_id="unique-key",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
            title="Original",
        )

        # Second registration with same key combo
        session2 = session_manager.register(
            external_id="unique-key",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
            title="Updated",
        )

        # Should be the same session with updated title
        assert session2.id == session1.id
        assert session2.title == "Updated"

    def test_get_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test getting a session by ID."""
        created = session_manager.register(
            external_id="get-test",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
        )

        retrieved = session_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.external_id == "get-test"

    def test_get_nonexistent(self, session_manager: LocalSessionManager) -> None:
        """Test getting nonexistent session returns None."""
        result = session_manager.get("nonexistent-id")
        assert result is None

    def test_find_by_external_id(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test finding session by external_id, machine_id, project_id, source."""
        session = session_manager.register(
            external_id="findable",
            machine_id="my-machine",
            source="claude",
            project_id=sample_project["id"],
        )

        found = session_manager.find_by_external_id(
            external_id="findable",
            machine_id="my-machine",
            project_id=sample_project["id"],
            source="claude",
        )

        assert found is not None
        assert found.id == session.id

    def test_find_by_external_id_not_found(self, session_manager: LocalSessionManager) -> None:
        """Test find_by_external_id returns None when not found."""
        result = session_manager.find_by_external_id(
            external_id="nonexistent",
            machine_id="machine",
            project_id="nonexistent-project",
            source="claude",
        )
        assert result is None

    def test_find_parent_no_handoff_ready(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test find_parent returns None when no handoff_ready session."""
        # Create an active session (not handoff_ready)
        session_manager.register(
            external_id="active-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.find_parent(
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        assert result is None

    def test_update_status(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session status."""
        session = session_manager.register(
            external_id="status-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        assert session.status == "active"

        updated = session_manager.update_status(session.id, "paused")
        assert updated is not None
        assert updated.status == "paused"

    def test_update_title(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session title."""
        session = session_manager.register(
            external_id="title-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_title(session.id, "New Title")
        assert updated is not None
        assert updated.title == "New Title"

    def test_update_title_schedules_tmux_rename(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Title changes propagate to tmux through the shared title update path."""
        session = session_manager.register(
            external_id="tmux-title-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={"tmux_pane": "%42"},
        )

        with patch("gobby.workflows.summary_actions.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.update_title(session.id, "Terminal Title")

        assert updated is not None
        mock_rename.assert_called_once()
        renamed_session, renamed_title = mock_rename.call_args.args
        assert renamed_session.id == session.id
        assert renamed_title == "Terminal Title"

    def test_update_title_can_update_source_without_renaming(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Changing provenance alone should not notify listeners or rename tmux."""
        session = session_manager.register(
            external_id="title-source-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Stable Title",
            terminal_context={"tmux_pane": "%42"},
        )
        session_manager.update(session.id, title_source="heuristic")
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        with patch("gobby.workflows.summary_actions.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.update_title(
                session.id,
                "Stable Title",
                title_source="llm",
            )

        assert updated is not None
        assert updated.title_source == "llm"
        assert calls == []
        mock_rename.assert_not_called()

    def test_update_title_notifies_listeners(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Listeners receive successful title changes."""
        session = session_manager.register(
            external_id="title-listener-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title(session.id, "Listener Title")

        assert updated is not None
        assert calls == [(session.id, "Listener Title")]

    def test_update_title_skips_noop_listener_notifications(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """No-op title updates do not notify listeners or rewrite updated_at."""
        session = session_manager.register(
            external_id="title-noop-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Stable Title",
        )
        original_updated_at = session.updated_at
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title(session.id, "Stable Title")

        assert updated is not None
        assert updated.title == "Stable Title"
        assert updated.updated_at == original_updated_at
        assert calls == []

    def test_update_title_missing_session_does_not_notify_listener(
        self,
        session_manager: LocalSessionManager,
    ) -> None:
        """Missing sessions return None and do not notify listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title("missing-session", "Nope")

        assert updated is None
        assert calls == []

    def test_update_title_listener_failure_does_not_break_update(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """One broken listener cannot block the title update or later listeners."""
        session = session_manager.register(
            external_id="title-failure-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []

        def _broken_listener(session_id: str, title: str) -> None:
            raise RuntimeError("listener failed")

        session_manager.register_title_listener(_broken_listener)
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title(session.id, "Recovered Title")

        assert updated is not None
        assert updated.title == "Recovered Title"
        assert calls == [(session.id, "Recovered Title")]

    def test_unregister_title_listener(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Unregistered listeners are not called."""
        session = session_manager.register(
            external_id="title-unregister-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []

        def _listener(session_id: str, title: str) -> None:
            calls.append((session_id, title))

        session_manager.register_title_listener(_listener)
        session_manager.unregister_title_listener(_listener)

        updated = session_manager.update_title(session.id, "Detached Title")

        assert updated is not None
        assert calls == []

    def test_update_stats(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session stats."""
        session = session_manager.register(
            external_id="stats-update-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_stats(
            session.id,
            message_count=10,
            turn_count=5,
            tool_call_count=3,
            last_assistant_content="Testing stats update.",
        )

        assert updated is not None
        assert updated.message_count == 10
        assert updated.turn_count == 5
        assert updated.tool_call_count == 3
        assert updated.last_assistant_content == "Testing stats update."

        # Verify DB persisted
        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session.id,))
        assert row["message_count"] == 10
        assert row["turn_count"] == 5
        assert row["tool_call_count"] == 3
        assert row["last_assistant_content"] == "Testing stats update."

    @pytest.mark.unit
    def test_update_model(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session model."""
        session = session_manager.register(
            external_id="model-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Initially model should be None
        assert session.model is None

        updated = session_manager.update_model(session.id, "claude-opus-4-5-20251101")
        assert updated is not None
        assert updated.model == "claude-opus-4-5-20251101"

    @pytest.mark.unit
    def test_create_web_chat_session_sets_model_and_chat_mode(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.create_web_chat_session(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
            title="Web Chat",
            model="claude-opus-4-5-20251101",
            chat_mode="accept_edits",
            sandbox_enabled=True,
            sandbox_policy_hash="policy-hash-123",
        )

        assert session.model == "claude-opus-4-5-20251101"
        assert session.chat_mode == "accept_edits"
        assert session.sandbox_enabled is True
        assert session.sandbox_policy_hash == "policy-hash-123"

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.model == "claude-opus-4-5-20251101"
        assert reloaded.chat_mode == "accept_edits"
        assert reloaded.sandbox_enabled is True
        assert reloaded.sandbox_policy_hash == "policy-hash-123"

    def test_update_summary(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session summary."""
        session = session_manager.register(
            external_id="summary-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_summary(
            session.id,
            summary_path="/path/to/summary.md",
            summary_markdown="# Summary\nThis is a test.",
        )

        assert updated is not None
        assert updated.summary_path == "/path/to/summary.md"
        assert updated.summary_markdown == "# Summary\nThis is a test."

    def test_list_sessions(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test listing sessions."""
        session_manager.register(
            external_id="list-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="list-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )

        sessions = session_manager.list(project_id=sample_project["id"])
        assert len(sessions) == 2

    def test_list_with_filters(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test listing sessions with filters."""
        s1 = session_manager.register(
            external_id="filter-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="filter-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        # Filter by source
        claude_sessions = session_manager.list(source="claude")
        assert len(claude_sessions) == 1
        assert claude_sessions[0].source == "claude"

        # Filter by status
        paused_sessions = session_manager.list(status="paused")
        assert len(paused_sessions) == 1
        assert paused_sessions[0].status == "paused"

    def test_list_with_limit(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test listing sessions with limit."""
        for i in range(5):
            session_manager.register(
                external_id=f"limit-{i}",
                machine_id=f"m{i}",
                source="claude",
                project_id=sample_project["id"],
            )

        sessions = session_manager.list(limit=3)
        assert len(sessions) == 3

    def test_delete_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test deleting a session."""
        session = session_manager.register(
            external_id="delete-me",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.delete(session.id)
        assert result is True
        assert session_manager.get(session.id) is None

    def test_delete_nonexistent(self, session_manager: LocalSessionManager) -> None:
        """Test deleting nonexistent session returns False."""
        result = session_manager.delete("nonexistent-id")
        assert result is False

    def test_expire_stale_sessions(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test expiring stale sessions."""
        # Create a stale session (simulated by mocking updated_at in query or just rely on db time)
        # Since we use SQLite datetime('now') in queries, we can't easily mock time without
        # deeper mocking. Instead, we'll verify the SQL generation and execution flow
        # or use a very short timeout.

        session = session_manager.register(
            external_id="stale-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Manually backdate the session in DB
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-25 hours') WHERE id = ?",
            (session.id,),
        )

        count = session_manager.expire_stale_sessions(timeout_hours=24)
        assert count == 1

        expired = session_manager.get(session.id)
        assert expired.status == "expired"

    def test_pause_inactive_active_sessions(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test pausing inactive active sessions."""
        session = session_manager.register(
            external_id="active-idle",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Backdate
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-31 minutes') WHERE id = ?",
            (session.id,),
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 1

        paused = session_manager.get(session.id)
        assert paused.status == "paused"

    def test_pause_inactive_active_sessions_preserves_last_activity_time(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Auto-pausing should not make a stale session look freshly active."""
        session = session_manager.register(
            external_id="active-idle-preserve-time",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-31 minutes') WHERE id = ?",
            (session.id,),
        )
        before = session_manager.db.fetchone(
            "SELECT updated_at FROM sessions WHERE id = ?",
            (session.id,),
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 1

        after = session_manager.db.fetchone(
            "SELECT updated_at FROM sessions WHERE id = ?",
            (session.id,),
        )
        assert before is not None
        assert after is not None
        assert after["updated_at"] == before["updated_at"]

    def test_pause_then_expire_stale_session_uses_last_activity_time(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """A very old session should expire in the same cleanup sweep after pause."""
        session = session_manager.register(
            external_id="ancient-active-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-25 hours') WHERE id = ?",
            (session.id,),
        )

        paused = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        expired = session_manager.expire_stale_sessions(timeout_hours=24)

        assert paused == 1
        assert expired == 1
        stale = session_manager.get(session.id)
        assert stale is not None
        assert stale.status == "expired"

    def test_transcript_processing_lifecycle(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test transcript processing lifecycle methods."""
        # Create expired session with transcript_path
        session = session_manager.register(
            external_id="transcript-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/test.jsonl",
        )
        session_manager.update_status(session.id, "expired")

        # Should be pending
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 1
        assert pending[0].id == session.id

        # Mark processed
        updated = session_manager.mark_transcript_processed(session.id)
        assert updated is not None
        # Verify it's no longer pending
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

        # Reset processed
        reset = session_manager.reset_transcript_processed(session.id)
        assert reset is not None

        # Should be pending again
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 1

    def test_update_parent_session_id(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating parent session ID."""
        session = session_manager.register(
            external_id="child",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        parent = session_manager.register(
            external_id="parent",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_parent_session_id(session.id, parent.id)
        assert updated is not None
        assert updated.parent_session_id == parent.id

    def test_storage_allows_self_parenting_without_guard(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """
        Test that storage layer allows setting a session as its own parent.

        This documents that the storage layer does NOT prevent self-parenting.
        The prevention logic is handled at the hook_manager level by not looking
        for parent sessions on 'compact' events, only on 'clear' events.

        This test verifies the storage behavior so we know the guard must be
        at a higher level.
        """
        # 1. Create a session
        session = session_manager.register(
            external_id="compact-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # 2. Mark it handoff_ready (simulating pre_compact)
        session_manager.update_status(session.id, "handoff_ready")

        # 3. Find parent - this finds the same session since it matches criteria
        parent = session_manager.find_parent(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
            status="handoff_ready",
        )

        # The storage layer finds the session as its own "parent"
        assert parent is not None
        assert parent.id == session.id  # Storage layer returns itself

        # 4. Verify storage layer allows self-parenting (no guard at this level)
        # This demonstrates that the hook_manager MUST prevent this case
        updated = session_manager.update_parent_session_id(session.id, session.id)
        assert updated is not None
        assert updated.parent_session_id == session.id  # Storage allows it

        # The fix for the self-parenting bug is in hook_manager.py:
        # - On 'compact' events: don't look for parent sessions
        # - On 'clear' events: look for handoff_ready sessions as parent
        # This test proves the storage layer has no guard, validating the
        # architecture decision to handle this at the hook_manager level.

    def test_find_parent_without_source_filter(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test find_parent without source filter finds any source."""
        session = session_manager.register(
            external_id="parent-any",
            machine_id="machine-1",
            source="gemini",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "handoff_ready")

        # Find without source filter
        found = session_manager.find_parent(
            machine_id="machine-1",
            project_id=sample_project["id"],
            source=None,  # No source filter
        )

        assert found is not None
        assert found.id == session.id

    def test_find_children(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test finding child sessions of a parent."""
        parent = session_manager.register(
            external_id="parent-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Create child sessions
        child1 = session_manager.register(
            external_id="child-1",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )
        child2 = session_manager.register(
            external_id="child-2",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )

        children = session_manager.find_children(parent.id)

        assert len(children) == 2
        child_ids = [c.id for c in children]
        assert child1.id in child_ids
        assert child2.id in child_ids

    def test_find_children_no_children(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test find_children returns empty list when no children."""
        session = session_manager.register(
            external_id="no-children",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        children = session_manager.find_children(session.id)
        assert children == []

    def test_update_multiple_fields(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating multiple session fields at once."""
        session = session_manager.register(
            external_id="multi-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Original Title",
        )

        updated = session_manager.update(
            session.id,
            external_id="new-ext-id",
            transcript_path="/new/path.jsonl",
            status="paused",
            title="New Title",
            git_branch="feature/branch",
        )

        assert updated is not None
        assert updated.external_id == "new-ext-id"
        assert updated.transcript_path == "/new/path.jsonl"
        assert updated.status == "paused"
        assert updated.title == "New Title"
        assert updated.git_branch == "feature/branch"

    def test_update_resume_metadata_fields(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session_type/model/chat_mode together for tmux resume."""
        session = session_manager.register(
            external_id="resume-ext",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            session_type="terminal",
        )

        updated = session_manager.update(
            session.id,
            source="codex",
            model="gpt-5.4",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
        )

        assert updated is not None
        assert updated.source == "codex"
        assert updated.model == "gpt-5.4"
        assert updated.chat_mode == "accept_edits"
        assert updated.session_type == "web_chat"
        assert updated.status == "active"

    def test_update_single_field(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating a single field."""
        session = session_manager.register(
            external_id="single-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, status="completed")

        assert updated is not None
        assert updated.status == "completed"

    def test_update_no_fields(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test update with no fields returns session unchanged."""
        session = session_manager.register(
            external_id="no-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.update(session.id)

        assert result is not None
        assert result.id == session.id

    def test_update_external_id_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just external_id."""
        session = session_manager.register(
            external_id="old-ext",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, external_id="new-ext")

        assert updated is not None
        assert updated.external_id == "new-ext"

    def test_update_transcript_path_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just transcript_path."""
        session = session_manager.register(
            external_id="jsonl-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, transcript_path="/updated/path.jsonl")

        assert updated is not None
        assert updated.transcript_path == "/updated/path.jsonl"

    def test_update_git_branch_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just git_branch."""
        session = session_manager.register(
            external_id="branch-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, git_branch="main")

        assert updated is not None
        assert updated.git_branch == "main"

    def test_count_sessions(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test counting sessions."""
        session_manager.register(
            external_id="count-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="count-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )

        count = session_manager.count(project_id=sample_project["id"])
        assert count == 2

    def test_count_with_filters(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test counting sessions with filters."""
        s1 = session_manager.register(
            external_id="count-filter-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="count-filter-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        # Count by source
        claude_count = session_manager.count(source="claude")
        assert claude_count == 1

        # Count by status
        paused_count = session_manager.count(status="paused")
        assert paused_count == 1

    def test_count_no_results(self, session_manager: LocalSessionManager) -> None:
        """Test count returns 0 when no sessions match."""
        count = session_manager.count(project_id="nonexistent-project")
        assert count == 0

    def test_count_by_status(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test counting sessions grouped by status."""
        s1 = session_manager.register(
            external_id="status-count-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        s2 = session_manager.register(
            external_id="status-count-2",
            machine_id="m2",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="status-count-3",
            machine_id="m3",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")
        session_manager.update_status(s2.id, "paused")

        counts = session_manager.count_by_status()

        # +1 active for the bootstrapped system session
        assert counts.get("active") == 2
        assert counts.get("paused") == 2

    def test_count_by_status_empty(self, session_manager: LocalSessionManager) -> None:
        """Test count_by_status with no user sessions (only bootstrapped system session)."""
        counts = session_manager.count_by_status()
        # The bootstrapped system session is always present
        assert counts == {"active": 1}

    def test_update_terminal_pickup_metadata(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating terminal pickup metadata."""
        session = session_manager.register(
            external_id="pickup-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Note: agent_run_id has a foreign key to agent_runs table
        # We test without agent_run_id to avoid FK constraint
        updated = session_manager.update_terminal_pickup_metadata(
            session.id,
            workflow_name="plan-execute",
            context_injected=True,
            original_prompt="Implement feature X",
        )

        assert updated is not None
        assert updated.workflow_name == "plan-execute"
        assert updated.context_injected is True
        assert updated.original_prompt == "Implement feature X"

    def test_update_terminal_pickup_metadata_partial(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating terminal pickup metadata with partial fields."""
        session = session_manager.register(
            external_id="partial-pickup",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Update only workflow_name
        updated = session_manager.update_terminal_pickup_metadata(
            session.id,
            workflow_name="test-driven",
        )

        assert updated is not None
        assert updated.workflow_name == "test-driven"
        assert updated.agent_run_id is None

    def test_update_terminal_pickup_metadata_no_fields(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test update_terminal_pickup_metadata with no fields returns session unchanged."""
        session = session_manager.register(
            external_id="no-pickup-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.update_terminal_pickup_metadata(session.id)

        assert result is not None
        assert result.id == session.id

    def test_update_terminal_pickup_context_injected_false(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating context_injected to False."""
        session = session_manager.register(
            external_id="context-false",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # First set to True
        session_manager.update_terminal_pickup_metadata(
            session.id,
            context_injected=True,
        )

        # Then set to False
        updated = session_manager.update_terminal_pickup_metadata(
            session.id,
            context_injected=False,
        )

        assert updated is not None
        assert updated.context_injected is False

    def test_expire_stale_sessions_no_stale(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test expire_stale_sessions returns 0 when no stale sessions."""
        session_manager.register(
            external_id="fresh-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        count = session_manager.expire_stale_sessions(timeout_hours=24)
        assert count == 0

    def test_pause_inactive_active_sessions_no_inactive(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test pause_inactive_active_sessions returns 0 when no inactive sessions."""
        session_manager.register(
            external_id="active-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 0

    def test_register_with_agent_depth_and_spawned_by(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test registering session with agent depth and spawned_by_agent_id."""
        session = session_manager.register(
            external_id="agent-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            agent_depth=2,
            spawned_by_agent_id="agent-abc",
        )

        assert session.agent_depth == 2
        assert session.spawned_by_agent_id == "agent-abc"

    def test_update_summary_partial(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating summary with only summary_path."""
        session = session_manager.register(
            external_id="summary-partial",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_summary(
            session.id,
            summary_path="/path/to/summary.md",
        )

        assert updated is not None
        assert updated.summary_path == "/path/to/summary.md"
        assert updated.summary_markdown is None

    def test_update_summary_markdown_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating summary with only summary_markdown."""
        session = session_manager.register(
            external_id="summary-md-only",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_summary(
            session.id,
            summary_markdown="# Just markdown",
        )

        assert updated is not None
        assert updated.summary_path is None
        assert updated.summary_markdown == "# Just markdown"

    def test_session_to_dict_includes_all_fields(
        self,
        session_manager: LocalSessionManager,
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

    def test_get_pending_transcript_sessions_with_limit(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test get_pending_transcript_sessions respects limit."""
        # Create multiple expired sessions with transcript_path
        for i in range(5):
            session = session_manager.register(
                external_id=f"pending-{i}",
                machine_id="machine",
                source="claude",
                project_id=sample_project["id"],
                transcript_path=f"/tmp/transcript-{i}.jsonl",
            )
            session_manager.update_status(session.id, "expired")

        pending = session_manager.get_pending_transcript_sessions(limit=3)
        assert len(pending) == 3

    def test_get_pending_transcript_sessions_excludes_processed(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes processed sessions."""
        session = session_manager.register(
            external_id="processed-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/transcript.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)

        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

    def test_get_pending_transcript_sessions_excludes_no_jsonl(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes sessions without transcript_path."""
        session = session_manager.register(
            external_id="no-jsonl-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path=None,  # No transcript path
        )
        session_manager.update_status(session.id, "expired")

        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

    def test_register_updates_metadata_on_existing_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register updates metadata when session exists."""
        # Create a parent session first for the foreign key
        parent = session_manager.register(
            external_id="parent-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # First registration without transcript_path or git_branch
        session1 = session_manager.register(
            external_id="update-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title=None,
            transcript_path=None,
            git_branch=None,
        )
        assert session1.transcript_path is None

        # Second registration with additional metadata
        session2 = session_manager.register(
            external_id="update-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Updated Title",
            transcript_path="/new/path.jsonl",
            git_branch="feature/new",
            parent_session_id=parent.id,  # Use real parent session
        )

        # Same session, updated metadata
        assert session2.id == session1.id
        assert session2.title == "Updated Title"
        assert session2.transcript_path == "/new/path.jsonl"
        assert session2.git_branch == "feature/new"
        assert session2.parent_session_id == parent.id
        assert session2.status == "active"  # Status reset to active

    def test_list_without_filters(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test listing all sessions without filters."""
        session_manager.register(
            external_id="list-all-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="list-all-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )

        sessions = session_manager.list()  # No filters
        assert len(sessions) >= 2


class TestSessionEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_register_raises_on_session_disappeared_during_update(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register raises RuntimeError if session disappears during update."""
        # Create initial session
        session_manager.register(
            external_id="disappearing-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Store the original find_by_external_id result
        existing = session_manager.find_by_external_id(
            "disappearing-session", "machine", sample_project["id"], "claude"
        )

        # Mock find_by_external_id to return the existing session (so we go into update path)
        # and mock get to return None (simulating the session disappearing)
        with patch.object(session_manager, "find_by_external_id", return_value=existing):
            with patch.object(session_manager, "get", return_value=None):
                with pytest.raises(RuntimeError, match="disappeared during update"):
                    session_manager.register(
                        external_id="disappearing-session",
                        machine_id="machine",
                        source="claude",
                        project_id=sample_project["id"],
                        title="Updated",
                    )

    def test_register_raises_on_session_not_found_after_creation(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register raises RuntimeError if session not found after creation."""
        # Mock get to return None after insert
        with patch.object(session_manager, "get", return_value=None):
            with patch.object(session_manager, "find_by_external_id", return_value=None):
                with pytest.raises(RuntimeError, match="not found after creation"):
                    session_manager.register(
                        external_id="ghost-session",
                        machine_id="machine",
                        source="claude",
                        project_id=sample_project["id"],
                    )

    def test_expire_stale_sessions_logs_when_sessions_expired(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that expire_stale_sessions logs when sessions are expired."""
        # Create a stale session
        session = session_manager.register(
            external_id="stale-log-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Backdate the session
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-25 hours') WHERE id = ?",
            (session.id,),
        )

        with patch("gobby.storage.session_lifecycle.logger") as mock_logger:
            count = session_manager.expire_stale_sessions(timeout_hours=24)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Expired 1 stale sessions" in mock_logger.info.call_args[0][0]

    def test_pause_inactive_sessions_logs_when_sessions_paused(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that pause_inactive_active_sessions logs when sessions are paused."""
        # Create an active session
        session = session_manager.register(
            external_id="pause-log-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Backdate the session
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-31 minutes') WHERE id = ?",
            (session.id,),
        )

        with patch("gobby.storage.session_lifecycle.logger") as mock_logger:
            count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Paused 1 inactive active sessions" in mock_logger.info.call_args[0][0]

    def test_register_logs_on_new_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register logs when creating a new session."""
        with patch("gobby.storage.sessions.logger") as mock_logger:
            session_manager.register(
                external_id="log-new-session",
                machine_id="machine",
                source="claude",
                project_id=sample_project["id"],
            )
            # Verify debug log was called for new session creation
            mock_logger.debug.assert_called()
            assert "Created new session" in str(mock_logger.debug.call_args_list[-1])

    def test_register_logs_on_reusing_existing_session(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register logs when reusing an existing session."""
        # Create initial session (without mocking logger)
        session_manager.register(
            external_id="log-reuse-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Now mock logger and register again
        with patch("gobby.storage.sessions.logger") as mock_logger:
            session_manager.register(
                external_id="log-reuse-session",
                machine_id="machine",
                source="claude",
                project_id=sample_project["id"],
                title="Updated",
            )
            # Verify debug log was called for reusing session
            mock_logger.debug.assert_called()
            assert "Reusing existing session" in str(mock_logger.debug.call_args_list[-1])

    def test_session_from_row_with_null_agent_depth(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test Session.from_row handles NULL agent_depth by defaulting to 0."""
        session = session_manager.register(
            external_id="null-depth",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Set agent_depth to NULL in database
        session_manager.db.execute(
            "UPDATE sessions SET agent_depth = NULL WHERE id = ?",
            (session.id,),
        )

        # Retrieve and verify default value
        retrieved = session_manager.get(session.id)
        assert retrieved is not None
        assert retrieved.agent_depth == 0

    def test_update_title_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just title via update method."""
        session = session_manager.register(
            external_id="title-only-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Original",
        )

        updated = session_manager.update(session.id, title="New Title Only")

        assert updated is not None
        assert updated.title == "New Title Only"

    def test_find_parent_returns_most_recent(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that find_parent returns the most recently updated session."""
        # Create first handoff_ready session
        session1 = session_manager.register(
            external_id="parent-1",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session1.id, "handoff_ready")

        # Backdate first session
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
            (session1.id,),
        )

        # Create second handoff_ready session (more recent)
        session2 = session_manager.register(
            external_id="parent-2",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session2.id, "handoff_ready")

        # Find parent - should return the more recent one
        parent = session_manager.find_parent(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
        )

        assert parent is not None
        assert parent.id == session2.id

    def test_count_with_all_filters(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test count with all three filters (project_id, status, source)."""
        s1 = session_manager.register(
            external_id="all-filters-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        session_manager.register(
            external_id="all-filters-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )

        # Count with all filters
        count = session_manager.count(
            project_id=sample_project["id"],
            status="paused",
            source="claude",
        )
        assert count == 1

    def test_list_with_all_filters(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test list with all three filters (project_id, status, source)."""
        s1 = session_manager.register(
            external_id="list-all-filters-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        session_manager.register(
            external_id="list-all-filters-2",
            machine_id="m2",
            source="gemini",
            project_id=sample_project["id"],
        )

        # List with all filters
        sessions = session_manager.list(
            project_id=sample_project["id"],
            status="paused",
            source="claude",
        )
        assert len(sessions) == 1
        assert sessions[0].id == s1.id

    def test_update_terminal_pickup_agent_run_id_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just agent_run_id in terminal pickup metadata.

        Note: agent_run_id has a foreign key constraint to agent_runs table.
        We test this by mocking the execute to verify the SQL is built correctly.
        """
        session = session_manager.register(
            external_id="agent-run-only",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Capture the SQL that would be executed
        original_execute = session_manager.db.execute
        executed_sql = []

        def capture_execute(sql, params=None):
            executed_sql.append((sql, params))
            return original_execute(sql, params)

        # Test by verifying the SQL generation (without executing against FK constraint)
        # The update_terminal_pickup_metadata builds dynamic SQL with agent_run_id
        with patch.object(session_manager.db, "execute", side_effect=capture_execute):
            # This will fail due to FK constraint, but we capture the SQL
            try:
                session_manager.update_terminal_pickup_metadata(
                    session.id,
                    agent_run_id="run-abc123",
                )
            except Exception:
                pass  # Expected FK constraint failure

        # Verify agent_run_id was included in the SQL
        assert any("agent_run_id" in sql for sql, _ in executed_sql)

    def test_update_terminal_pickup_original_prompt_only(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating just original_prompt in terminal pickup metadata."""
        session = session_manager.register(
            external_id="prompt-only",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_terminal_pickup_metadata(
            session.id,
            original_prompt="Implement feature X",
        )

        assert updated is not None
        assert updated.original_prompt == "Implement feature X"
        assert updated.workflow_name is None


@pytest.mark.unit
class TestProjectScopedSeqNum:
    """Tests for project-scoped session seq_num feature."""

    def test_seq_num_per_project(
        self,
        session_manager: LocalSessionManager,
        temp_db,
    ) -> None:
        """Test that seq_num is assigned per project, not globally."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = proj_manager.create(name="project1", repo_path="/tmp/p1")
        project2 = proj_manager.create(name="project2", repo_path="/tmp/p2")

        # Create sessions in project1
        s1_p1 = session_manager.register(
            external_id="s1-p1", machine_id="m1", source="claude", project_id=project1.id
        )
        s2_p1 = session_manager.register(
            external_id="s2-p1", machine_id="m1", source="claude", project_id=project1.id
        )

        # Create sessions in project2
        s1_p2 = session_manager.register(
            external_id="s1-p2", machine_id="m1", source="claude", project_id=project2.id
        )

        # Project1 sessions should have seq_num 1 and 2
        assert s1_p1.seq_num == 1
        assert s2_p1.seq_num == 2

        # Project2 session should have seq_num 1 (independent from project1)
        assert s1_p2.seq_num == 1

    def test_resolve_session_reference_with_project_id(
        self,
        session_manager: LocalSessionManager,
        temp_db,
    ) -> None:
        """Test resolving #N format with project_id parameter."""
        from gobby.storage.projects import LocalProjectManager

        proj_manager = LocalProjectManager(temp_db)
        project1 = proj_manager.create(name="proj1", repo_path="/tmp/proj1")
        project2 = proj_manager.create(name="proj2", repo_path="/tmp/proj2")

        # Create #1 in each project
        s1 = session_manager.register(
            external_id="s1", machine_id="m1", source="claude", project_id=project1.id
        )
        s2 = session_manager.register(
            external_id="s2", machine_id="m1", source="claude", project_id=project2.id
        )

        # Resolve #1 with project1 context
        resolved1 = session_manager.resolve_session_reference("#1", project_id=project1.id)
        assert resolved1 == s1.id

        # Resolve #1 with project2 context
        resolved2 = session_manager.resolve_session_reference("#1", project_id=project2.id)
        assert resolved2 == s2.id

    def test_resolve_session_reference_requires_project_id_for_seq_num(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that resolve_session_reference raises ValueError for #N without project_id."""
        session = session_manager.register(
            external_id="global-test",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )

        # Without project_id, #N resolution must fail (seq_num is per-project)
        with pytest.raises(ValueError, match="project context is required"):
            session_manager.resolve_session_reference(f"#{session.seq_num}")

    def test_resolve_session_reference_uuid_format(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test that UUID format still works with project-scoped resolution."""
        session = session_manager.register(
            external_id="uuid-test",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )

        # UUID should resolve regardless of project_id
        resolved = session_manager.resolve_session_reference(session.id)
        assert resolved == session.id

    def test_resolve_session_reference_not_found(
        self,
        session_manager: LocalSessionManager,
        sample_project: dict,
    ) -> None:
        """Test ValueError raised when session not found."""
        with pytest.raises(ValueError, match="not found"):
            session_manager.resolve_session_reference("#999", project_id=sample_project["id"])
