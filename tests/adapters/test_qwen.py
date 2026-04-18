"""Tests for Qwen CLI adapter."""

import pytest

from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookResponse, SessionSource

pytestmark = pytest.mark.unit


class TestQwenAdapter:
    """Qwen-specific regression coverage for inherited Gemini behavior."""

    def test_source_is_qwen(self) -> None:
        adapter = QwenAdapter()
        assert adapter.source == SessionSource.QWEN

    def test_session_start_routes_banner_once(self) -> None:
        adapter = QwenAdapter()
        banner = "Gobby Session ID: #42 (uuid-123)"

        response = HookResponse(decision="allow", system_message=banner)
        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert result["hookSpecificOutput"]["additionalContext"].count(banner) == 1

    def test_session_start_banner_and_metadata_include_session_id_once(self) -> None:
        adapter = QwenAdapter()
        banner = "Gobby Session ID: #42 (uuid-123)"

        response = HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#42",
                "external_id": "qwen-ext-id",
                "_first_hook_for_session": True,
                "project_id": "proj-xyz",
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert ctx.count(banner) == 1
        assert "qwen-ext-id" in ctx
        assert "proj-xyz" in ctx
