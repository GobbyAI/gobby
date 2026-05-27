"""Focused tests for session storage behavior."""

from unittest.mock import patch

import pytest
from psycopg.errors import RaiseException

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionManagerMetadata:
    """Tests split from the SessionManager storage monolith."""

    def test_update_title(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
    ) -> None:
        """Missing sessions return None and do not notify listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title("missing-session", "Nope")

        assert updated is None
        assert calls == []

    def test_update_digest_markdown_missing_session_does_not_notify_listener(
        self,
        session_manager: SessionManager,
    ) -> None:
        """Missing digest updates return None and do not notify session listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        updated = session_manager.update_digest_markdown("missing-session", "## Digest")

        assert updated is None
        assert calls == []

    def test_persist_digest_state_updates_digest_and_title_atomically(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Digest persistence updates all digest fields and title metadata in one call."""
        session = session_manager.register(
            external_id="digest-state-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Old Title",
            terminal_context={"tmux_pane": "%42"},
        )
        session_calls: list[tuple[str, str]] = []
        title_calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: session_calls.append((event, session_id))
        )
        session_manager.register_title_listener(
            lambda session_id, title: title_calls.append((session_id, title))
        )

        with patch("gobby.workflows.summary_actions.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.persist_digest_state(
                session.id,
                last_turn_markdown="last turn",
                digest_markdown="### Turn 1\nlast turn",
                last_digest_input_hash="abc123",
                title="Digest Title",
                title_source="llm",
            )

        assert updated is not None
        assert updated.title == "Digest Title"
        assert updated.title_source == "llm"
        assert updated.last_turn_markdown == "last turn"
        assert updated.digest_markdown == "### Turn 1\nlast turn"
        assert updated.last_digest_input_hash == "abc123"
        assert session_calls == [("session_updated", session.id)]
        assert title_calls == [(session.id, "Digest Title")]
        mock_rename.assert_called_once()

    def test_persist_digest_state_missing_session_does_not_notify_listener(
        self,
        session_manager: SessionManager,
    ) -> None:
        """Missing digest-state updates return None and do not notify listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        updated = session_manager.persist_digest_state(
            "missing-session",
            last_turn_markdown="last turn",
            digest_markdown="digest",
            last_digest_input_hash="abc123",
            title="Digest Title",
            title_source="llm",
        )

        assert updated is None
        assert calls == []

    def test_persist_digest_state_rolls_back_on_update_failure(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """A failed digest update leaves title and digest columns unchanged."""
        session = session_manager.register(
            external_id="digest-rollback-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Old Title",
        )
        session_manager.db.execute(
            """
            CREATE OR REPLACE FUNCTION fail_digest_state_update_fn()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.digest_markdown = 'boom' THEN
                    RAISE EXCEPTION 'digest boom';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        session_manager.db.execute(
            """
            CREATE TRIGGER fail_digest_state_update
            BEFORE UPDATE OF digest_markdown ON sessions
            FOR EACH ROW
            EXECUTE FUNCTION fail_digest_state_update_fn()
            """
        )

        with pytest.raises(RaiseException, match="digest boom"):
            session_manager.persist_digest_state(
                session.id,
                last_turn_markdown="new turn",
                digest_markdown="boom",
                last_digest_input_hash="new-hash",
                title="New Title",
                title_source="llm",
            )

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.title == "Old Title"
        assert reloaded.title_source is None
        assert reloaded.last_turn_markdown is None
        assert reloaded.digest_markdown is None
        assert reloaded.last_digest_input_hash is None

    def test_update_title_listener_failure_does_not_break_update(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_session_change_listener_fires_for_register(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Session creation notifies generic session change listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        session = session_manager.register(
            external_id="change-register-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        assert calls == [("session_created", session.id)]

    def test_session_change_listener_fires_for_web_chat_create(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Web chat creation uses the shared creation notification path."""
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        session = session_manager.create_web_chat_session(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
            title="Web Chat",
            model="sonnet",
            chat_mode="plan",
            sandbox_enabled=True,
            sandbox_policy_hash="policy",
        )

        assert calls == [("session_created", session.id)]

    def test_session_change_listener_fires_for_existing_session_reactivation(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Re-registering an existing session notifies as an update."""
        session = session_manager.register(
            external_id="change-reactivation-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )
        session_manager.update_status(session.id, "paused")
        calls.clear()

        updated = session_manager.register(
            external_id="change-reactivation-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        assert updated.id == session.id
        assert updated.status == "active"
        assert calls == [("session_updated", session.id)]

    def test_session_change_listener_fires_for_status_updates(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Status changes emit update or expired events."""
        session = session_manager.register(
            external_id="change-status-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        session_manager.update_status(session.id, "paused")
        session_manager.update_status(session.id, "expired")

        assert calls == [
            ("session_updated", session.id),
            ("session_expired", session.id),
        ]

    def test_session_change_listener_fires_for_metadata_updates(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Title, model, summary, digest, last turn, and bulk updates notify listeners."""
        session = session_manager.register(
            external_id="change-metadata-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        session_manager.update_title(session.id, "Changed Title")
        session_manager.update_model(session.id, "sonnet")
        session_manager.update_summary(session.id, summary_markdown="# Summary")
        session_manager.update_digest_markdown(session.id, "## Digest")
        session_manager.update_last_turn_markdown(session.id, "last turn")
        session_manager.update(session.id, git_branch="feature/session-events")

        assert calls == [
            ("session_updated", session.id),
            ("session_updated", session.id),
            ("session_updated", session.id),
            ("session_updated", session.id),
            ("session_updated", session.id),
            ("session_updated", session.id),
        ]

    def test_session_change_listener_fires_for_delete(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Deleting a session notifies catalog listeners."""
        session = session_manager.register(
            external_id="change-delete-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        deleted = session_manager.delete(session.id)

        assert deleted is True
        assert calls == [("session_deleted", session.id)]

    def test_session_change_listener_failure_does_not_break_mutation(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """One broken generic listener cannot block the mutation or later listeners."""
        session = session_manager.register(
            external_id="change-failure-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        calls: list[tuple[str, str]] = []

        def _broken_listener(event: str, session_id: str) -> None:
            raise RuntimeError("listener failed")

        session_manager.register_session_change_listener(_broken_listener)
        session_manager.register_session_change_listener(
            lambda event, session_id: calls.append((event, session_id))
        )

        updated = session_manager.update_title(session.id, "Recovered Title")

        assert updated is not None
        assert updated.title == "Recovered Title"
        assert calls == [("session_updated", session.id)]

    def test_update_stats(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_update_summary(
        self,
        session_manager: SessionManager,
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

    def test_update_resume_metadata_fields(
        self,
        session_manager: SessionManager,
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

    def test_update_resume_metadata_reuses_conflicting_web_chat_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Terminal-to-web resume is idempotent when the web chat row already exists."""
        terminal = session_manager.register(
            external_id="resume-dupe",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            session_type="terminal",
            title="Terminal",
        )
        web_chat = session_manager.register(
            external_id="resume-dupe",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            session_type="web_chat",
            title="Existing web chat",
        )

        updated = session_manager.update(
            terminal.id,
            model="gpt-5.4",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
            title="Resumed web chat",
        )

        terminal_reloaded = session_manager.get(terminal.id)
        assert updated is not None
        assert updated.id == web_chat.id
        assert updated.session_type == "web_chat"
        assert updated.model == "gpt-5.4"
        assert updated.chat_mode == "accept_edits"
        assert updated.status == "active"
        assert updated.title == "Resumed web chat"
        assert terminal_reloaded is not None
        assert terminal_reloaded.session_type == "terminal"

    def test_update_terminal_pickup_metadata(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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

    def test_update_summary_partial(
        self,
        session_manager: SessionManager,
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
        session_manager: SessionManager,
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
