"""Tests for HTTP request and response models."""

import pytest

from gobby.servers.models import SessionRegisterRequest, WebChatSessionRequest

pytestmark = pytest.mark.unit


class TestSessionRegisterRequest:
    """Tests for SessionRegisterRequest model."""

    def test_required_fields(self) -> None:
        """Test that external_id is required."""
        request = SessionRegisterRequest(
            external_id="test-key",
            machine_id=None,
            transcript_path=None,
            title=None,
            source=None,
            parent_session_id=None,
            status=None,
            project_id=None,
            project_path=None,
            git_branch=None,
            cwd=None,
        )
        assert request.external_id == "test-key"

    def test_optional_fields(self) -> None:
        """Test all optional fields."""
        request = SessionRegisterRequest(
            external_id="test-key",
            machine_id="machine-123",
            transcript_path="/path/to/transcript.jsonl",
            title="Test Session",
            source="Claude Code",
            parent_session_id="parent-uuid",
            status="active",
            project_id="project-uuid",
            project_path="/path/to/project",
            git_branch="main",
            cwd="/current/working/dir",
        )

        assert request.machine_id == "machine-123"
        assert request.title == "Test Session"
        assert request.git_branch == "main"


class TestWebChatSessionRequest:
    """Tests for WebChatSessionRequest model."""

    def test_defaults(self) -> None:
        request = WebChatSessionRequest()

        assert request.provider == "claude"
        assert request.project_id is None
        assert request.cwd is None
        assert request.title is None
        assert request.model is None
        assert request.chat_mode is None

    def test_optional_fields(self) -> None:
        request = WebChatSessionRequest(
            provider="codex",
            project_id="project-uuid",
            cwd="/repo",
            title="Web Chat",
            model="gpt-5.4",
            chat_mode="plan",
        )

        assert request.provider == "codex"
        assert request.project_id == "project-uuid"
        assert request.cwd == "/repo"
        assert request.title == "Web Chat"
        assert request.model == "gpt-5.4"
        assert request.chat_mode == "plan"
