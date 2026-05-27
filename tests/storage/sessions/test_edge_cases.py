"""Focused tests for session storage behavior."""

from unittest.mock import patch

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_register_raises_on_session_disappeared_during_update(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        with patch("gobby.storage.session_lifecycle.logger") as mock_logger:
            count = session_manager.expire_stale_sessions(timeout_hours=24)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Expired 1 stale sessions" in mock_logger.info.call_args[0][0]

    def test_pause_inactive_sessions_logs_when_sessions_paused(
        self,
        session_manager: SessionManager,
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
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '31 minutes' WHERE id = %s",
            (session.id,),
        )

        with patch("gobby.storage.session_lifecycle.logger") as mock_logger:
            count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
            assert count == 1
            mock_logger.info.assert_called_once()
            assert "Paused 1 inactive active sessions" in mock_logger.info.call_args[0][0]

    def test_register_logs_on_new_session(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
            "UPDATE sessions SET agent_depth = NULL WHERE id = %s",
            (session.id,),
        )

        # Retrieve and verify default value
        retrieved = session_manager.get(session.id)
        assert retrieved is not None
        assert retrieved.agent_depth == 0

    def test_update_title_only(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '1 hour' WHERE id = %s",
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
