"""Focused tests for session storage behavior."""


import pytest

from gobby.storage.sessions import SYSTEM_SESSION_ID, SessionManager

pytestmark = pytest.mark.unit


class TestSessionManagerRegistration:
    """Tests split from the SessionManager storage monolith."""

    def test_register_session(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_get_nonexistent(self, session_manager: SessionManager) -> None:
        """Test getting nonexistent session returns None."""
        result = session_manager.get("nonexistent-id")
        assert result is None

    def test_find_by_external_id(
        self,
        session_manager: SessionManager,
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

    def test_find_by_external_id_not_found(self, session_manager: SessionManager) -> None:
        """Test find_by_external_id returns None when not found."""
        result = session_manager.find_by_external_id(
            external_id="nonexistent",
            machine_id="machine",
            project_id="nonexistent-project",
            source="claude",
        )
        assert result is None

    @pytest.mark.unit
    def test_create_web_chat_session_sets_model_and_chat_mode(
        self,
        session_manager: SessionManager,
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

    def test_register_with_agent_depth_and_spawned_by(
        self,
        session_manager: SessionManager,
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

    def test_register_updates_metadata_on_existing_session(
        self,
        session_manager: SessionManager,
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

