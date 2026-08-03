"""Focused tests for session storage behavior."""

import uuid
from unittest.mock import patch

import pytest
from psycopg.errors import RaiseException

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

# sessions.id is a native uuid column; a valid-but-unknown UUID exercises the
# "missing session" path without tripping the uuid cast.
MISSING_SESSION_ID = str(uuid.uuid4())


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
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_title(session.id, "New Title")
        assert updated is not None
        assert updated.title == "New Title"
        assert updated.title_source == "manual"

    def test_update_title_schedules_tmux_rename(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Title changes propagate to tmux through the shared title update path."""
        session = session_manager.register(
            external_id="tmux-title-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={"tmux_pane": "%42"},
        )

        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
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
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={"tmux_pane": "%42"},
        )
        assert session.title is not None
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.update_title(
                session.id,
                session.title,
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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

    @pytest.mark.parametrize("owner", ["llm", "manual"])
    def test_update_title_rejects_provisional_downgrades(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        owner: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"direct-title-precedence-{owner}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        owned = session_manager.update_title(
            session.id,
            f"{owner} title",
            title_source=owner,
        )
        assert owned is not None

        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )
        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
            session_manager.update_title(
                session.id,
                "Fallback title",
                title_source="provisional",
            )
            session_manager.update_title(
                session.id,
                f"{owner} title",
                title_source="provisional",
            )
            updated = session_manager.update_title(
                session.id,
                "",
                title_source="provisional",
            )

        assert updated is not None
        assert updated.title == f"{owner} title"
        assert updated.title_source == owner
        assert calls == []
        mock_rename.assert_not_called()

    @pytest.mark.parametrize("owner", ["llm", "manual"])
    def test_bulk_update_rejects_title_downgrades_but_persists_other_fields(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        owner: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"bulk-title-precedence-{owner}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        owned = session_manager.update_title(
            session.id,
            f"{owner} title",
            title_source=owner,
        )
        assert owned is not None

        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )
        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
            session_manager.update(session.id, title_source="provisional")
            session_manager.update(session.id, title=None, title_source=None)
            updated = session_manager.update(
                session.id,
                title="Fallback title",
                title_source="provisional",
                model="new-model",
            )

        assert updated is not None
        assert updated.title == f"{owner} title"
        assert updated.title_source == owner
        assert updated.model == "new-model"
        assert calls == []
        mock_rename.assert_not_called()

    def test_update_title_missing_session_does_not_notify_listener(
        self,
        session_manager: SessionManager,
    ) -> None:
        """Missing sessions return None and do not notify listeners."""
        calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: calls.append((session_id, title))
        )

        updated = session_manager.update_title(MISSING_SESSION_ID, "Nope")

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

        updated = session_manager.update_digest_markdown(MISSING_SESSION_ID, "## Digest")

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
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
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

        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.persist_digest_state(
                session.id,
                last_turn_markdown="last turn",
                digest_markdown="### Turn 1\nlast turn",
                last_digest_input_hash="abc123",
                last_digested_pair_index=3,
                title="Digest Title",
                title_source="llm",
            )

        assert updated is not None
        assert updated.title == "Digest Title"
        assert updated.title_source == "llm"
        assert updated.last_turn_markdown == "last turn"
        assert updated.digest_markdown == "### Turn 1\nlast turn"
        assert updated.last_digest_input_hash == "abc123"
        assert updated.last_digested_pair_index == 3
        assert session_calls == [("session_updated", session.id)]
        assert title_calls == [(session.id, "Digest Title")]
        mock_rename.assert_called_once()

    def test_persist_digest_state_preserves_manual_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Digest state lands while a user-owned title remains unchanged."""
        session = session_manager.register(
            external_id="manual-title-digest-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            title="My Title",
        )
        title_calls: list[tuple[str, str]] = []
        session_manager.register_title_listener(
            lambda session_id, title: title_calls.append((session_id, title))
        )

        with patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename") as mock_rename:
            updated = session_manager.persist_digest_state(
                session.id,
                last_turn_markdown="last turn",
                digest_markdown="### Turn 1\nlast turn",
                last_digest_input_hash="abc123",
                last_digested_pair_index=1,
                title="Digest Title",
                title_source="llm",
            )

        assert updated is not None
        assert updated.title == "My Title"
        assert updated.title_source == "manual"
        assert updated.digest_markdown == "### Turn 1\nlast turn"
        assert title_calls == []
        mock_rename.assert_not_called()

    def test_persist_digest_state_refreshes_llm_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="llm-title-refresh-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        first = session_manager.persist_digest_state(
            session.id,
            last_turn_markdown="first turn",
            digest_markdown="### Turn 1\nfirst turn",
            last_digest_input_hash="first-hash",
            last_digested_pair_index=1,
            title="First digest title",
            title_source="llm",
        )
        assert first is not None

        updated = session_manager.persist_digest_state(
            session.id,
            last_turn_markdown="second turn",
            digest_markdown="### Turn 2\nsecond turn",
            last_digest_input_hash="second-hash",
            last_digested_pair_index=2,
            title="Refreshed digest title",
            title_source="llm",
        )

        assert updated is not None
        assert updated.title == "Refreshed digest title"
        assert updated.title_source == "llm"

    def test_manual_rename_overrides_llm_and_blocks_later_digest(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="manual-after-llm-title-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        digest_owned = session_manager.update_title(
            session.id,
            "Digest title",
            title_source="llm",
        )
        assert digest_owned is not None

        manual = session_manager.update_title(session.id, "User title")
        assert manual is not None
        updated = session_manager.persist_digest_state(
            session.id,
            last_turn_markdown="last turn",
            digest_markdown="### Turn 1\nlast turn",
            last_digest_input_hash="digest-hash",
            last_digested_pair_index=1,
            title="Later digest title",
            title_source="llm",
        )

        assert updated is not None
        assert updated.title == "User title"
        assert updated.title_source == "manual"

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
            MISSING_SESSION_ID,
            last_turn_markdown="last turn",
            digest_markdown="digest",
            last_digest_input_hash="abc123",
            last_digested_pair_index=1,
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
            machine_id="20000000-0000-4000-8000-000000000001",
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

        try:
            with pytest.raises(RaiseException, match="digest boom"):
                session_manager.persist_digest_state(
                    session.id,
                    last_turn_markdown="new turn",
                    digest_markdown="boom",
                    last_digest_input_hash="new-hash",
                    last_digested_pair_index=4,
                    title="New Title",
                    title_source="llm",
                )

            reloaded = session_manager.get(session.id)
            assert reloaded is not None
            assert reloaded.title == "Old Title"
            assert reloaded.title_source == "manual"
            assert reloaded.last_turn_markdown is None
            assert reloaded.digest_markdown is None
            assert reloaded.last_digest_input_hash is None
            assert reloaded.last_digested_pair_index == 0
        finally:
            session_manager.db.execute(
                "DROP TRIGGER IF EXISTS fail_digest_state_update ON sessions"
            )
            session_manager.db.execute("DROP FUNCTION IF EXISTS fail_digest_state_update_fn()")

    def test_update_title_listener_failure_does_not_break_update(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """One broken listener cannot block the title update or later listeners."""
        session = session_manager.register(
            external_id="title-failure-test",
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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

    def test_status_transition_listener_failure_does_not_break_committed_update(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="broken-status-listener-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        transitions: list[SessionStatusTransition] = []

        def broken_listener(_transition: SessionStatusTransition) -> None:
            raise RuntimeError("listener failed")

        session_manager.register_status_transition_listener(broken_listener)
        session_manager.register_status_transition_listener(transitions.append)

        updated = session_manager.update_status(session.id, "paused")

        assert updated is not None
        assert updated.status == "paused"
        persisted = session_manager.get(session.id)
        assert persisted is not None
        assert persisted.status == "paused"
        assert [transition.status for transition in transitions] == ["paused"]

    def test_update_stats(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session stats."""
        session = session_manager.register(
            external_id="stats-update-test",
            machine_id="20000000-0000-4000-8000-000000000001",
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
        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = %s", (session.id,))
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
        assert updated.summary_revision_id is not None
        assert updated.summary_generation_mode == "agent_authored"
        assert updated.summary_source_context_hash is None
        assert updated.summary_digest_turn_count is None

        revisions = session_manager.list_summary_revisions(session.id)
        assert len(revisions) == 1
        assert revisions[0]["id"] == updated.summary_revision_id
        assert revisions[0]["generation_mode"] == "agent_authored"
        assert revisions[0]["summary_markdown"] == "# Summary\nThis is a test."
        assert revisions[0]["metadata_json"] == {"source": "update_summary"}

        cleared = session_manager.update_summary(
            session.id,
            summary_path=None,
            summary_markdown=None,
        )

        assert cleared is not None
        assert cleared.summary_path is None
        assert cleared.summary_markdown is None
        assert cleared.summary_revision_id is None
        assert cleared.summary_generation_mode is None

    def test_persist_summary_state_updates_revision_and_session_atomically(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Summary state persistence writes the revision and session metadata together."""
        session = session_manager.register(
            external_id="summary-state-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        first = session_manager.persist_summary_state(
            session.id,
            summary_markdown="# First",
            generation_mode="full",
            source_context_hash="hash-1",
            source_digest_turn_count=3,
            metadata_json={"reason": "missing_summary_metadata"},
        )
        second = session_manager.persist_summary_state(
            session.id,
            summary_markdown="# Second",
            generation_mode="delta",
            source_context_hash="hash-2",
            source_digest_turn_count=4,
            metadata_json={"reason": "safe_digest_watermark"},
        )

        assert first is not None
        assert second is not None
        assert first.summary_revision_id is not None
        assert second.summary_revision_id is not None
        assert second.summary_revision_id != first.summary_revision_id
        assert second.summary_markdown == "# Second"
        assert second.summary_source_context_hash == "hash-2"
        assert second.summary_digest_turn_count == 4
        assert second.summary_generation_mode == "delta"
        assert second.summary_generated_at is not None

        revisions = session_manager.list_summary_revisions(session.id)
        assert [revision["generation_mode"] for revision in revisions] == ["delta", "full"]
        assert revisions[0]["previous_revision_id"] == first.summary_revision_id
        assert revisions[0]["metadata_json"] == {"reason": "safe_digest_watermark"}
        fetched = session_manager.get_summary_revision(second.summary_revision_id)
        assert fetched == revisions[0]

    def test_persist_summary_state_rolls_back_revision_on_update_failure(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """A failed session update leaves no orphaned summary revision."""
        session = session_manager.register(
            external_id="summary-state-rollback-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.db.execute(
            """
            CREATE OR REPLACE FUNCTION fail_summary_state_update_fn()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.summary_markdown = 'boom' THEN
                    RAISE EXCEPTION 'summary boom';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        session_manager.db.execute(
            """
            CREATE TRIGGER fail_summary_state_update
            BEFORE UPDATE OF summary_markdown ON sessions
            FOR EACH ROW
            EXECUTE FUNCTION fail_summary_state_update_fn()
            """
        )

        try:
            with pytest.raises(RaiseException, match="summary boom"):
                session_manager.persist_summary_state(
                    session.id,
                    summary_markdown="boom",
                    generation_mode="full",
                    source_context_hash="hash-1",
                    source_digest_turn_count=1,
                )

            reloaded = session_manager.get(session.id)
            assert reloaded is not None
            assert reloaded.summary_markdown is None
            assert reloaded.summary_revision_id is None
            assert session_manager.list_summary_revisions(session.id) == []
        finally:
            session_manager.db.execute(
                "DROP TRIGGER IF EXISTS fail_summary_state_update ON sessions"
            )
            session_manager.db.execute("DROP FUNCTION IF EXISTS fail_summary_state_update_fn()")

    def test_update_resume_metadata_fields(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test updating session_type/model/chat_mode together for tmux resume."""
        session = session_manager.register(
            external_id="resume-ext",
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
            session_type="terminal",
            title="Terminal",
        )
        web_chat = session_manager.register(
            external_id="resume-dupe",
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
        sample_project: dict[str, str],
    ) -> None:
        """Test update_terminal_pickup_metadata with no fields returns session unchanged."""
        session = session_manager.register(
            external_id="no-pickup-update",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.update_terminal_pickup_metadata(session.id)

        assert result is not None
        assert result.id == session.id

    def test_update_terminal_pickup_context_injected_false(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating context_injected to False."""
        session = session_manager.register(
            external_id="context-false",
            machine_id="20000000-0000-4000-8000-000000000001",
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
        sample_project: dict[str, str],
    ) -> None:
        """Test updating summary with only summary_path."""
        session = session_manager.register(
            external_id="summary-partial",
            machine_id="20000000-0000-4000-8000-000000000001",
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
        assert updated.summary_revision_id is None
        assert session_manager.list_summary_revisions(session.id) == []

    def test_update_summary_path_only_uses_transaction_connection(self) -> None:
        from gobby.storage.sessions._summary_update import _SummaryUpdateMixin

        class FakeConn:
            def __init__(self) -> None:
                self.executed: list[tuple[str, tuple[object, ...]]] = []

            def execute(self, sql: str, params: tuple[object, ...]) -> None:
                self.executed.append((sql, params))

        class FakeDb:
            def __init__(self) -> None:
                self.conn = FakeConn()

            def transaction(self) -> "FakeDb":
                return self

            def __enter__(self) -> FakeConn:
                return self.conn

            def __exit__(self, *_exc: object) -> None:
                return None

            def execute(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("update must execute through transaction connection")

        class Manager(_SummaryUpdateMixin):
            def __init__(self) -> None:
                self.db = FakeDb()

            def get(self, _session_id: str) -> None:
                return None

            def _notify_session_change(self, _event: str, _session_id: str) -> None:
                return None

        manager = Manager()

        assert manager.update_summary("session-1", summary_path="/tmp/summary.md") is None
        assert manager.db.conn.executed

    def test_update_summary_markdown_only(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating summary with only summary_markdown."""
        session = session_manager.register(
            external_id="summary-md-only",
            machine_id="20000000-0000-4000-8000-000000000001",
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


class TestHandoffTitlePrecedence:
    def test_handoff_title_replaces_provisional_title(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="handoff-title-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_title(
            session.id,
            "Handoff Title",
            title_source="handoff",
        )

        assert updated is not None
        assert updated.title == "Handoff Title"
        assert updated.title_source == "handoff"

    @pytest.mark.parametrize("owner", ["llm", "manual"])
    def test_stronger_title_ownership_replaces_handoff(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        owner: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"stronger-than-handoff-{owner}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.update_title(
            session.id,
            "Handoff Title",
            title_source="handoff",
        )

        updated = session_manager.update_title(
            session.id,
            f"{owner} title",
            title_source=owner,
        )

        assert updated is not None
        assert updated.title == f"{owner} title"
        assert updated.title_source == owner

    @pytest.mark.parametrize("owner", ["llm", "manual"])
    def test_handoff_cannot_downgrade_stronger_title_ownership(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        owner: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"handoff-downgrade-{owner}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.update_title(
            session.id,
            f"{owner} title",
            title_source=owner,
        )

        updated = session_manager.update_title(
            session.id,
            "Late Handoff",
            title_source="handoff",
        )

        assert updated is not None
        assert updated.title == f"{owner} title"
        assert updated.title_source == owner
