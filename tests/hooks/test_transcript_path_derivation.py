"""Transcript path derivation tests for event handlers."""

from __future__ import annotations

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestTranscriptPathDerivation:
    """Test _derive_transcript_path and helpers for non-Claude CLIs."""

    def test_derive_transcript_path_unknown_cli(self, event_handlers: EventHandlers) -> None:
        """Unknown CLI should return None."""
        result = event_handlers._derive_transcript_path("unknown-cli", {}, "ext-123")
        assert result is None

    def test_derive_transcript_path_claude_returns_none(
        self, event_handlers: EventHandlers
    ) -> None:
        """Claude provides transcript_path natively, so derivation returns None."""
        result = event_handlers._derive_transcript_path("claude", {}, "ext-123")
        assert result is None

    def test_find_gemini_transcript_no_cwd(self, event_handlers: EventHandlers) -> None:
        """Should return None when cwd is not provided."""
        result = event_handlers._find_gemini_transcript({}, "ext-123")
        assert result is None

    def test_find_gemini_transcript_nonexistent_dir(self, event_handlers: EventHandlers) -> None:
        """Should return None when chats dir doesn't exist."""
        result = event_handlers._find_gemini_transcript(
            {"cwd": "/nonexistent/path/that/does/not/exist"}, "ext-123"
        )
        assert result is None

    def test_find_gemini_transcript_by_prefix(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Should find Gemini session file by session_id prefix."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".gemini" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create a session file matching the prefix
        session_file = chats_dir / "session-2024-01-01T10-00-abcd1234.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_gemini_transcript(
            {"cwd": cwd, "session_id": "abcd1234-full-uuid"}, "ext-123"
        )
        assert result is not None
        assert "abcd1234" in result

    def test_find_gemini_transcript_fallback_most_recent(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Should fall back to most recent session file when prefix doesn't match."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".gemini" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create session files that won't match the prefix
        session_file = chats_dir / "session-2024-01-01T10-00-xxxxxxxx.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_gemini_transcript(
            {"cwd": cwd, "session_id": "nomatch-uuid"}, "ext-123"
        )
        assert result is not None
        assert "xxxxxxxx" in result

    def test_derive_gemini_dispatches(self, event_handlers: EventHandlers) -> None:
        """_derive_transcript_path should dispatch to _find_gemini_transcript for gemini."""
        # Without cwd, gemini derivation returns None
        result = event_handlers._derive_transcript_path("gemini", {}, "ext-123")
        assert result is None

    def test_derive_grok_transcript_path_uses_encoded_cwd(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Grok updates.jsonl path should match the native session layout."""
        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._derive_transcript_path(
            "grok",
            {"cwd": "/repo/my project", "sessionId": "grok-session"},
            "external-id",
        )

        assert result == str(
            tmp_path
            / ".grok"
            / "sessions"
            / "%2Frepo%2Fmy%20project"
            / "grok-session"
            / "updates.jsonl"
        )

    def test_session_start_derives_gemini_transcript(self, mock_dependencies: dict) -> None:
        """SESSION_START should derive transcript_path for Gemini when not provided natively."""
        handlers = EventHandlers(**mock_dependencies)
        mock_dependencies["session_storage"].get.return_value = None

        event = make_event(
            HookEventType.SESSION_START,
            source="gemini",
            data={"cwd": "/some/project", "source": "startup"},
        )

        response = handlers.handle_session_start(event)
        assert response.decision == "allow"
