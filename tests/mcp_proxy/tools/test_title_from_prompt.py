"""Tests for synthesize_title_from_prompt MCP tool.

Verifies:
- Tool registration on gobby-sessions registry
- Guard clauses: short prompts, slash commands, existing titles
- LLM call and title sanitization
- Session update and tmux rename
- Error handling (missing LLM service, timeout, missing session)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._actions import _sanitize_title
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


# ─── Fixtures ───


@pytest.fixture
def mock_session() -> MagicMock:
    """A session with no title (needs synthesis)."""
    session = MagicMock()
    session.id = "sess-123"
    session.title = None
    session.terminal_context = {"tmux_pane": "%42"}
    session.agent_depth = 0
    return session


@pytest.fixture
def mock_session_manager(mock_session) -> MagicMock:
    manager = MagicMock()
    manager.get = MagicMock(return_value=mock_session)
    manager.update_title = MagicMock(return_value=mock_session)
    return manager


@pytest.fixture
def mock_llm_provider() -> MagicMock:
    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value="Implement Auth System")
    return provider


@pytest.fixture
def mock_llm_service(mock_llm_provider) -> MagicMock:
    service = MagicMock()
    service.get_provider_for_feature = MagicMock(return_value=(mock_llm_provider, "haiku", None))
    service.get_default_provider = MagicMock(return_value=mock_llm_provider)
    service.call_feature = AsyncMock(return_value="Implement Auth System")
    return service


@pytest.fixture
def mock_config() -> MagicMock:
    from gobby.config.sessions import SessionTitleConfig

    config = MagicMock()
    config.session_title = SessionTitleConfig()
    return config


@pytest.fixture
def registry(mock_session_manager, mock_llm_service, mock_config) -> InternalToolRegistry:
    return create_session_messages_registry(
        session_manager=mock_session_manager,
        llm_service=mock_llm_service,
        config=mock_config,
        db=None,  # None triggers lazy LocalDatabase in PromptLoader; we patch load() instead
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool registration
# ═══════════════════════════════════════════════════════════════════════


class TestToolRegistration:
    def test_tool_registered(self, registry) -> None:
        assert "synthesize_title_from_prompt" in registry._tools


# ═══════════════════════════════════════════════════════════════════════
# Guard clauses
# ═══════════════════════════════════════════════════════════════════════


class TestGuardClauses:
    @pytest.mark.asyncio
    async def test_skips_short_prompt(self, registry) -> None:
        with session_context_for_test("sess-123"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {"prompt_text": "hi"},
            )
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "prompt_too_short"

    @pytest.mark.asyncio
    async def test_skips_empty_prompt(self, registry) -> None:
        with session_context_for_test("sess-123"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {"prompt_text": ""},
            )
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "prompt_too_short"

    @pytest.mark.asyncio
    async def test_skips_slash_command(self, registry) -> None:
        with session_context_for_test("sess-123"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {"prompt_text": "/commit -m 'fix stuff'"},
            )
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "slash_command"

    @pytest.mark.asyncio
    async def test_skips_existing_title(self, registry, mock_session, mock_session_manager) -> None:
        mock_session.title = "Already Has a Title"
        mock_session_manager.get.return_value = mock_session

        with session_context_for_test("sess-123"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {
                    "prompt_text": "Help me build a REST API",
                },
            )
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "title_already_set"

    @pytest.mark.asyncio
    async def test_does_not_skip_untitled_session(
        self, registry, mock_session, mock_session_manager
    ) -> None:
        """'Untitled Session' is treated as default and should be overwritten."""
        mock_session.title = "Untitled Session"
        mock_session_manager.get.return_value = mock_session

        with patch(
            "gobby.prompts.loader.PromptLoader.load",
            side_effect=FileNotFoundError("not found"),
        ):
            with session_context_for_test("sess-123"):
                result = await registry.call(
                    "synthesize_title_from_prompt",
                    {
                        "prompt_text": "Help me build a REST API",
                    },
                )
        assert result["success"] is True
        assert "skipped" not in result
        assert "title" in result


# ═══════════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    @pytest.fixture(autouse=True)
    def _patch_prompt_loader(self):
        """All happy-path tests bypass PromptLoader (hits inline fallback)."""
        with patch(
            "gobby.prompts.loader.PromptLoader.load",
            side_effect=FileNotFoundError("not found"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_synthesizes_title(
        self,
        registry,
        mock_session_manager,
        mock_llm_provider,
    ) -> None:
        with session_context_for_test("sess-123"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {
                    "prompt_text": "Help me implement user authentication with JWT tokens",
                },
            )
        assert result["success"] is True
        assert result["title"] == "Implement Auth System"
        mock_session_manager.update_title.assert_called_once_with(
            "sess-123", "Implement Auth System"
        )

    @pytest.mark.asyncio
    async def test_calls_llm_with_prompt(
        self,
        registry,
        mock_llm_service,
    ) -> None:
        with session_context_for_test("sess-123"):
            await registry.call(
                "synthesize_title_from_prompt",
                {
                    "prompt_text": "Refactor the database layer to use connection pooling",
                },
            )
        mock_llm_service.call_feature.assert_awaited_once()
        call_kwargs = mock_llm_service.call_feature.call_args
        assert call_kwargs.kwargs["max_tokens"] == 30

    @pytest.mark.asyncio
    async def test_renames_tmux_window(
        self,
        registry,
        mock_session_manager,
        mock_session,
    ) -> None:
        with patch(
            "gobby.workflows.summary_actions._rename_tmux_window",
            new_callable=AsyncMock,
        ) as mock_rename:
            with session_context_for_test("sess-123"):
                result = await registry.call(
                    "synthesize_title_from_prompt",
                    {
                        "prompt_text": "Add dark mode support to the frontend",
                    },
                )
            assert result["success"] is True
            mock_rename.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_missing_session(self, registry, mock_session_manager) -> None:
        mock_session_manager.get.return_value = None
        with session_context_for_test("nonexistent"):
            result = await registry.call(
                "synthesize_title_from_prompt",
                {
                    "prompt_text": "Help me debug this error",
                },
            )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_no_llm_service(self, mock_session_manager) -> None:
        """Registry created without llm_service should return error."""
        reg = create_session_messages_registry(
            session_manager=mock_session_manager,
            llm_service=None,
            db=None,
        )
        with session_context_for_test("sess-123"):
            result = await reg.call(
                "synthesize_title_from_prompt",
                {
                    "prompt_text": "Help me build a REST API",
                },
            )
        assert result["success"] is False
        assert "LLM service" in result["error"]

    @pytest.mark.asyncio
    async def test_llm_timeout(self, registry, mock_llm_service) -> None:
        mock_llm_service.call_feature = AsyncMock(side_effect=TimeoutError("timed out"))
        with patch(
            "gobby.prompts.loader.PromptLoader.load",
            side_effect=FileNotFoundError("not found"),
        ):
            with session_context_for_test("sess-123"):
                result = await registry.call(
                    "synthesize_title_from_prompt",
                    {
                        "prompt_text": "Help me build a REST API with pagination",
                    },
                )
        assert result["success"] is False
        assert "timed out" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# _sanitize_title unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestSanitizeTitle:
    def test_strips_quotes(self) -> None:
        assert _sanitize_title('"My Title"') == "My Title"
        assert _sanitize_title("'My Title'") == "My Title"

    def test_strips_markdown(self) -> None:
        assert _sanitize_title("## My **Bold** Title") == "My Bold Title"

    def test_strips_emoji(self) -> None:
        result = _sanitize_title("Fix Bug \U0001f41b in Auth")
        assert "\U0001f41b" not in result
        assert "Fix Bug" in result

    def test_truncates_long_titles(self) -> None:
        long_title = "A" * 100
        result = _sanitize_title(long_title)
        assert len(result) <= 80
        assert result.endswith("...")

    def test_takes_first_line(self) -> None:
        assert _sanitize_title("First Line\nSecond Line") == "First Line"

    def test_normalizes_whitespace(self) -> None:
        assert _sanitize_title("Too   Many    Spaces") == "Too Many Spaces"

    def test_empty_returns_untitled(self) -> None:
        assert _sanitize_title("") == "Untitled Session"
        assert _sanitize_title("   ") == "Untitled Session"

    def test_passthrough_clean_title(self) -> None:
        assert _sanitize_title("Implement Auth System") == "Implement Auth System"
