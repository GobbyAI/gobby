"""Transcript path derivation tests for event handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType, HookResponse

from ._event_handler_helpers import empty_database_mock, make_event

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


class TestTranscriptPathDerivation:
    """Test _derive_transcript_path and helpers for non-Claude CLIs."""

    def test_derive_transcript_path_unknown_cli(self, event_handlers: EventHandlers) -> None:
        """Unknown CLI should return None."""
        result = event_handlers._derive_transcript_path(
            "unknown-cli",
            {},
            "ext-123",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        assert result is None

    def test_derive_transcript_path_claude_returns_none(
        self, event_handlers: EventHandlers
    ) -> None:
        """Claude provides transcript_path natively, so derivation returns None."""
        result = event_handlers._derive_transcript_path(
            "claude",
            {},
            "ext-123",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        assert result is None

    def test_find_qwen_transcript_no_cwd(self, event_handlers: EventHandlers) -> None:
        """Should return None when cwd is not provided."""
        result = event_handlers._find_qwen_transcript({}, "ext-123")
        assert result is None

    def test_find_qwen_transcript_nonexistent_dir(self, event_handlers: EventHandlers) -> None:
        """Should return None when chats dir doesn't exist."""
        result = event_handlers._find_qwen_transcript(
            {"cwd": "/nonexistent/path/that/does/not/exist"}, "ext-123"
        )
        assert result is None

    def test_find_qwen_transcript_by_prefix(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Should find Qwen session file by session_id prefix."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create a session file matching the prefix
        session_file = chats_dir / "session-2024-01-01T10-00-abcd1234.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_qwen_transcript(
            {"cwd": cwd, "session_id": "abcd1234-full-uuid"}, "ext-123"
        )
        assert result is not None
        assert "abcd1234" in result

    def test_find_qwen_transcript_prefix_miss_does_not_use_prior_session(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """A prefix miss must not bind another Qwen session's transcript."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create a prior session file that must never be used for this session.
        session_file = chats_dir / "session-2024-01-01T10-00-xxxxxxxx.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_qwen_transcript(
            {"cwd": cwd, "session_id": "nomatch-uuid"}, "ext-123"
        )
        assert result is None

    def test_find_qwen_transcript_can_retry_after_initial_prefix_miss(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """A later hook can derive the exact transcript after Qwen creates it."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        prior_session = chats_dir / "session-2024-01-01T10-00-xxxxxxxx.json"
        prior_session.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))
        input_data = {"cwd": cwd, "session_id": "abcd1234-full-uuid"}

        assert event_handlers._find_qwen_transcript(input_data, "ext-123") is None

        current_session = chats_dir / "session-2024-01-01T10-01-abcd1234.json"
        current_session.touch()

        assert event_handlers._find_qwen_transcript(input_data, "ext-123") == str(current_session)

    def test_before_agent_registers_qwen_transcript_created_after_session_start(
        self,
        mock_dependencies: dict[str, Any],
        tmp_path,
        monkeypatch,
    ) -> None:
        """The first later hook persists and registers the exact Qwen transcript."""
        import hashlib
        from types import SimpleNamespace

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        prior_session = chats_dir / "session-2024-01-01T10-00-xxxxxxxx.json"
        prior_session.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))
        handlers = EventHandlers(**mock_dependencies)
        monkeypatch.setattr(handlers, "_inject_agent_instructions_if_needed", lambda *_args: None)
        session_manager = mock_dependencies["session_manager"]
        session_manager.get.return_value = SimpleNamespace(
            id="platform-session",
            machine_id=LOCAL_MACHINE_ID,
            transcript_path=None,
        )
        monkeypatch.setattr(
            "gobby.sessions.machine_scope.get_machine_id",
            lambda: LOCAL_MACHINE_ID,
        )
        input_data = {
            "cwd": cwd,
            "session_id": "abcd1234-full-uuid",
            "prompt": "continue",
        }

        assert handlers._find_qwen_transcript(input_data, "external-qwen") is None

        current_session = chats_dir / "session-2024-01-01T10-01-abcd1234.json"
        current_session.touch()
        event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="external-qwen",
            source="qwen",
            data=input_data,
            metadata={"_platform_session_id": "platform-session"},
        )

        response = handlers.handle_before_agent(event)

        assert response.decision == "allow"
        session_manager.update.assert_called_once_with(
            "platform-session", transcript_path=str(current_session)
        )
        mock_dependencies["session_coordinator"].register_session.assert_called_once_with(
            "external-qwen"
        )
        mock_dependencies["message_processor_resolver"]().register_session.assert_called_once_with(
            "platform-session", str(current_session), source="qwen"
        )
        assert (
            handlers._session_message_processors["platform-session"]
            is mock_dependencies["message_processor_resolver"]()
        )

    def test_derive_qwen_dispatches(self, event_handlers: EventHandlers) -> None:
        """_derive_transcript_path should dispatch to _find_qwen_transcript for qwen."""
        # Without cwd, qwen derivation returns None
        result = event_handlers._derive_transcript_path(
            "qwen",
            {},
            "ext-123",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        assert result is None

    def test_derive_grok_transcript_path_uses_encoded_cwd(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Grok updates.jsonl path should match the native session layout."""
        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))
        target = (
            tmp_path
            / ".grok"
            / "sessions"
            / "%2Frepo%2Fmy%20project"
            / "grok-session"
            / "updates.jsonl"
        )
        target.parent.mkdir(parents=True)
        target.write_text("{}\n", encoding="utf-8")

        result = event_handlers._derive_transcript_path(
            "grok",
            {"cwd": "/repo/my project", "sessionId": "grok-session"},
            "external-id",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )

        assert result == str(target)

    @pytest.mark.usefixtures("mock_empty_session_variable_manager")
    def test_session_start_derives_qwen_transcript(
        self,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """SESSION_START should derive transcript_path for Qwen when not provided natively."""
        handlers = EventHandlers(**mock_dependencies)
        mock_dependencies["session_storage"].get.return_value = None

        event = make_event(
            HookEventType.SESSION_START,
            source="qwen",
            data={"cwd": "/some/project", "source": "startup"},
        )

        response = handlers.handle_session_start(event)
        assert response.decision == "allow"

    def test_derive_classifies_hook_path_usable_pending_invalid(
        self, event_handlers: EventHandlers, tmp_path: Path
    ) -> None:
        usable = tmp_path / "usable.jsonl"
        usable.write_text("{}\n", encoding="utf-8")
        pending = tmp_path / "pending.jsonl"
        unreadable_dir = tmp_path / "not-a-file"
        unreadable_dir.mkdir()

        assert event_handlers._derive_transcript_path(
            "claude",
            {"transcript_path": str(usable)},
            "ext-1",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        ) == str(usable)
        assert (
            event_handlers._derive_transcript_path(
                "claude",
                {"transcript_path": str(pending)},
                "ext-1",
                owner_machine_id=LOCAL_MACHINE_ID,
                local_machine_id=LOCAL_MACHINE_ID,
            )
            is None
        )
        assert (
            event_handlers._derive_transcript_path(
                "claude",
                {"transcript_path": str(unreadable_dir)},
                "ext-1",
                owner_machine_id=LOCAL_MACHINE_ID,
                local_machine_id=LOCAL_MACHINE_ID,
            )
            is None
        )

    @pytest.mark.usefixtures("mock_empty_session_variable_manager")
    def test_session_start_does_not_persist_pending_agy_path(
        self,
        mock_dependencies: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        from gobby.storage.sessions._update_sentinel import UNSET

        pending = (
            tmp_path
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / "conv-1"
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
        handlers = EventHandlers(**mock_dependencies, get_machine_id=lambda: LOCAL_MACHINE_ID)
        existing = SimpleNamespace(
            id="sess-1",
            machine_id=LOCAL_MACHINE_ID,
            transcript_path=None,
            project_id="proj-1",
            agent_run_id=None,
            session_type="terminal",
            parent_session_id=None,
            workflow_name=None,
            terminal_context=None,
        )
        event = make_event(
            HookEventType.SESSION_START,
            session_id="conv-1",
            source="agy",
            data={
                "cwd": str(tmp_path),
                "source": "startup",
                "transcript_path": str(pending),
            },
        )
        event.machine_id = LOCAL_MACHINE_ID
        mock_dependencies["session_manager"].update.return_value = existing
        mock_dependencies["session_manager"].db = empty_database_mock()

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch.object(
                handlers,
                "_compose_session_response",
                return_value=HookResponse(decision="allow"),
            ),
        ):
            response = handlers._handle_pre_created_session(
                existing_session=cast(Any, existing),
                external_id="conv-1",
                transcript_path=str(pending),
                cli_source="agy",
                event=event,
                cwd=str(tmp_path),
            )

        assert response.decision == "allow"
        mock_dependencies["session_manager"].update.assert_called_with(
            session_id="sess-1",
            transcript_path=UNSET,
            status="active",
        )

    @pytest.mark.usefixtures("mock_empty_session_variable_manager")
    def test_handle_session_start_does_not_persist_pending_hook_path(
        self,
        mock_dependencies: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
        from gobby.hooks.project_context import HookProjectResolution

        pending = tmp_path / "pending.jsonl"
        handlers = EventHandlers(**mock_dependencies, get_machine_id=lambda: LOCAL_MACHINE_ID)
        session_manager = cast(MagicMock, handlers._session_manager)
        session_manager.get.return_value = None
        session_manager.find_by_external_id.return_value = None
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-pending",
            source="claude",
            data={
                "cwd": str(tmp_path),
                "source": "startup",
                "transcript_path": str(pending),
            },
        )
        event.machine_id = LOCAL_MACHINE_ID

        with (
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_hook_project_context",
                return_value=HookProjectResolution("proj-1"),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=SessionStartResolution(session=None, session_source="startup"),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.session_start_should_defer",
                return_value=False,
            ),
            patch.object(handlers, "_activate_materialized_session", return_value=[]),
            patch.object(
                handlers,
                "_compose_session_response",
                return_value=HookResponse(decision="allow"),
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        session_manager.register_session.assert_called()
        assert session_manager.register_session.call_args.kwargs["transcript_path"] is None

    def test_derive_agy_disk_fallback_uses_transcript_full(
        self, event_handlers: EventHandlers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = (
            tmp_path
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / "conv-1"
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
        target.parent.mkdir(parents=True)
        target.write_text("{}\n", encoding="utf-8")
        (target.parent / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

        result = event_handlers._derive_transcript_path(
            "agy",
            {},
            "conv-1",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        assert result == str(target)
