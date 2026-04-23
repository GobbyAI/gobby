"""Focused tests for session storage behavior."""

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionManagerLifecycle:
    """Tests split from the SessionManager storage monolith."""

    def test_update_status(
        self,
        session_manager: SessionManager,
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

    def test_list_sessions(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_delete_nonexistent(self, session_manager: SessionManager) -> None:
        """Test deleting nonexistent session returns False."""
        result = session_manager.delete("nonexistent-id")
        assert result is False

    def test_transcript_processing_lifecycle(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_find_children(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_update_single_field(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_count_no_results(self, session_manager: SessionManager) -> None:
        """Test count returns 0 when no sessions match."""
        count = session_manager.count(project_id="nonexistent-project")
        assert count == 0

    def test_count_by_status(
        self,
        session_manager: SessionManager,
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

    def test_count_by_status_empty(self, session_manager: SessionManager) -> None:
        """Test count_by_status with no user sessions (only bootstrapped system session)."""
        counts = session_manager.count_by_status()
        # The bootstrapped system session is always present
        assert counts == {"active": 1}

    def test_list_without_filters(
        self,
        session_manager: SessionManager,
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
