"""Tests for GeminiExecutor class."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.llm.executor import ToolResult, ToolSchema

if TYPE_CHECKING:
    from gobby.llm.gemini_executor import GeminiExecutor

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_genai_module():
    """Mock the google.genai module."""
    mock_genai = MagicMock()
    mock_types = MagicMock()
    mock_genai.types = mock_types
    mock_genai.Client = MagicMock()

    # Mock types used in executor
    mock_types.GenerateContentConfig = MagicMock()
    mock_types.Content = MagicMock()
    mock_types.Part = MagicMock()
    mock_types.Part.from_text = MagicMock()
    mock_types.Part.from_function_response = MagicMock()
    mock_types.Tool = MagicMock()
    mock_types.FunctionDeclaration = MagicMock()

    mock_google = MagicMock()
    mock_google.genai = mock_genai

    with patch.dict(
        sys.modules,
        {
            "google": mock_google,
            "google.genai": mock_genai,
            "google.genai.types": mock_types,
        },
    ):
        yield mock_genai


@pytest.fixture
def simple_tools() -> list[ToolSchema]:
    """Create simple tool schemas for testing."""
    return [
        ToolSchema(
            name="get_weather",
            description="Get the current weather",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"},
                },
                "required": ["location"],
            },
        ),
    ]


class TestGeminiExecutorInit:
    """Tests for GeminiExecutor initialization."""

    def test_init_api_key_mode(self, mock_genai_module) -> None:
        """GeminiExecutor initializes with api_key mode."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="api_key", api_key="test-key")
        assert executor.auth_mode == "api_key"
        assert executor.api_key == "test-key"
        assert executor.default_model == "gemini-2.0-flash"

    def test_init_adc_mode(self, mock_genai_module) -> None:
        """GeminiExecutor initializes with adc mode."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="adc", project="my-project", location="us-east1")
        assert executor.auth_mode == "adc"
        assert executor.project == "my-project"
        assert executor.location == "us-east1"

    def test_init_default_model(self, mock_genai_module) -> None:
        """GeminiExecutor uses custom default model."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(default_model="gemini-2.5-pro", api_key="test-key")
        assert executor.default_model == "gemini-2.5-pro"

    def test_init_default_location(self, mock_genai_module) -> None:
        """GeminiExecutor defaults location to us-central1."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="adc")
        assert executor.location == "us-central1"

    def test_provider_name(self, mock_genai_module) -> None:
        """Provider name is 'gemini'."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(api_key="test-key")
        assert executor.provider_name == "gemini"

    def test_init_api_key_mode_requires_key(self, mock_genai_module) -> None:
        """GeminiExecutor raises ValueError when api_key mode has no key."""
        import pytest

        from gobby.llm.gemini_executor import GeminiExecutor

        with pytest.raises(ValueError, match="api_key is required"):
            GeminiExecutor(auth_mode="api_key")


class TestGeminiExecutorClient:
    """Tests for lazy client initialization."""

    def test_lazy_client_api_key(self, mock_genai_module) -> None:
        """Client is lazily created for api_key mode."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="api_key", api_key="test-key")
        assert executor._client is None

        client = executor._get_client()
        mock_genai_module.Client.assert_called_once_with(api_key="test-key")
        assert client is not None

    def test_lazy_client_adc(self, mock_genai_module) -> None:
        """Client is lazily created for adc mode with Vertex AI."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="adc", project="my-project", location="us-west1")
        client = executor._get_client()
        mock_genai_module.Client.assert_called_once_with(
            vertexai=True, project="my-project", location="us-west1"
        )
        assert client is not None

    def test_client_cached(self, mock_genai_module) -> None:
        """Client is created only once."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="api_key", api_key="test-key")
        client1 = executor._get_client()
        client2 = executor._get_client()
        assert client1 is client2
        assert mock_genai_module.Client.call_count == 1


class TestGeminiExecutorToolConversion:
    """Tests for tool schema conversion."""

    def test_convert_tools(self, mock_genai_module, simple_tools) -> None:
        """Tools are converted to genai format."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(api_key="test-key")
        result = executor._convert_tools(simple_tools)

        # Should return a list with one Tool containing declarations
        assert len(result) == 1
        mock_genai_module.types.FunctionDeclaration.assert_called_once()
        mock_genai_module.types.Tool.assert_called_once()


class TestGeminiExecutorRun:
    """Tests for the agentic loop."""

    @pytest.fixture
    def executor(self, mock_genai_module) -> GeminiExecutor:
        """Create a GeminiExecutor with mocked client."""
        from gobby.llm.gemini_executor import GeminiExecutor

        executor = GeminiExecutor(auth_mode="api_key", api_key="test-key")
        return executor

    @pytest.mark.asyncio
    async def test_run_simple_response(self, executor, simple_tools, mock_genai_module) -> None:
        """Run returns text response when no function calls."""
        # Mock response with text only (no function calls)
        mock_part = MagicMock()
        mock_part.function_call = None
        mock_part.text = "Hello from Gemini!"

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 10
        mock_response.usage_metadata.candidates_token_count = 5

        # Set up async mock
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        executor._client = mock_client

        async def dummy_handler(name, args):
            return ToolResult(tool_name=name, success=True)

        result = await executor.run(prompt="Hello", tools=simple_tools, tool_handler=dummy_handler)

        assert result.status == "success"
        assert result.output == "Hello from Gemini!"
        assert result.turns_used == 1
        assert result.cost_info is not None
        assert result.cost_info.prompt_tokens == 10
        assert result.cost_info.completion_tokens == 5

    @pytest.mark.asyncio
    async def test_run_with_function_call(self, executor, simple_tools, mock_genai_module) -> None:
        """Run handles function calls and sends results back."""
        # First response: function call
        mock_fc = MagicMock()
        mock_fc.name = "get_weather"
        mock_fc.args = {"location": "Seattle"}

        fc_part = MagicMock()
        fc_part.function_call = mock_fc
        fc_part.text = None

        fc_content = MagicMock()
        fc_content.parts = [fc_part]

        fc_candidate = MagicMock()
        fc_candidate.content = fc_content

        fc_response = MagicMock()
        fc_response.candidates = [fc_candidate]
        fc_response.usage_metadata = MagicMock()
        fc_response.usage_metadata.prompt_token_count = 20
        fc_response.usage_metadata.candidates_token_count = 10

        # Second response: text (done)
        text_part = MagicMock()
        text_part.function_call = None
        text_part.text = "The weather in Seattle is rainy."

        text_content = MagicMock()
        text_content.parts = [text_part]

        text_candidate = MagicMock()
        text_candidate.content = text_content

        text_response = MagicMock()
        text_response.candidates = [text_candidate]
        text_response.usage_metadata = MagicMock()
        text_response.usage_metadata.prompt_token_count = 30
        text_response.usage_metadata.candidates_token_count = 15

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=[fc_response, text_response]
        )
        executor._client = mock_client

        async def tool_handler(name, args):
            return ToolResult(tool_name=name, success=True, result={"temp": "55F"})

        result = await executor.run(
            prompt="Weather?", tools=simple_tools, tool_handler=tool_handler
        )

        assert result.status == "success"
        assert result.output == "The weather in Seattle is rainy."
        assert result.turns_used == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "get_weather"
        assert result.tool_calls[0].arguments == {"location": "Seattle"}
        # Token counts accumulate
        assert result.cost_info is not None
        assert result.cost_info.prompt_tokens == 50
        assert result.cost_info.completion_tokens == 25

    @pytest.mark.asyncio
    async def test_run_api_error(self, executor, simple_tools, mock_genai_module) -> None:
        """Run returns error status on API failure."""
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=Exception("API quota exceeded")
        )
        executor._client = mock_client

        async def dummy_handler(name, args):
            return ToolResult(tool_name=name, success=True)

        result = await executor.run(prompt="Hello", tools=simple_tools, tool_handler=dummy_handler)

        assert result.status == "error"
        assert "API quota exceeded" in result.error

    @pytest.mark.asyncio
    async def test_run_max_turns(self, executor, simple_tools, mock_genai_module) -> None:
        """Run returns partial status when max turns reached."""
        # Always return a function call
        mock_fc = MagicMock()
        mock_fc.name = "get_weather"
        mock_fc.args = {"location": "NYC"}

        fc_part = MagicMock()
        fc_part.function_call = mock_fc
        fc_part.text = None

        fc_content = MagicMock()
        fc_content.parts = [fc_part]

        fc_candidate = MagicMock()
        fc_candidate.content = fc_content

        fc_response = MagicMock()
        fc_response.candidates = [fc_candidate]
        fc_response.usage_metadata = MagicMock()
        fc_response.usage_metadata.prompt_token_count = 10
        fc_response.usage_metadata.candidates_token_count = 5

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=fc_response)
        executor._client = mock_client

        async def tool_handler(name, args):
            return ToolResult(tool_name=name, success=True, result={"temp": "72F"})

        result = await executor.run(
            prompt="Weather?",
            tools=simple_tools,
            tool_handler=tool_handler,
            max_turns=2,
        )

        assert result.status == "partial"
        assert result.turns_used == 2

    @pytest.mark.asyncio
    async def test_run_no_candidates(self, executor, simple_tools, mock_genai_module) -> None:
        """Run handles empty candidates response."""
        mock_response = MagicMock()
        mock_response.candidates = []
        mock_response.usage_metadata = None

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        executor._client = mock_client

        async def dummy_handler(name, args):
            return ToolResult(tool_name=name, success=True)

        result = await executor.run(prompt="Hello", tools=simple_tools, tool_handler=dummy_handler)

        assert result.status == "error"
        assert "No candidates" in result.error

    @pytest.mark.asyncio
    async def test_run_tool_handler_error(self, executor, simple_tools, mock_genai_module) -> None:
        """Run handles tool handler exceptions gracefully."""
        mock_fc = MagicMock()
        mock_fc.name = "get_weather"
        mock_fc.args = {"location": "Seattle"}

        fc_part = MagicMock()
        fc_part.function_call = mock_fc
        fc_part.text = None

        fc_content = MagicMock()
        fc_content.parts = [fc_part]
        fc_candidate = MagicMock()
        fc_candidate.content = fc_content
        fc_response = MagicMock()
        fc_response.candidates = [fc_candidate]
        fc_response.usage_metadata = None

        # Second response: done
        text_part = MagicMock()
        text_part.function_call = None
        text_part.text = "Sorry, error occurred."
        text_content = MagicMock()
        text_content.parts = [text_part]
        text_candidate = MagicMock()
        text_candidate.content = text_content
        text_response = MagicMock()
        text_response.candidates = [text_candidate]
        text_response.usage_metadata = None

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=[fc_response, text_response]
        )
        executor._client = mock_client

        async def failing_handler(name, args):
            raise RuntimeError("Tool crashed")

        result = await executor.run(
            prompt="Weather?", tools=simple_tools, tool_handler=failing_handler
        )

        assert result.status == "success"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].result is not None
        assert result.tool_calls[0].result.success is False
