"""Tests for OpenAIExecutor class."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.llm.executor import ToolResult, ToolSchema

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_openai_module():
    """Mock the openai module."""
    mock_openai = MagicMock()
    mock_openai.AsyncOpenAI = MagicMock()

    with patch.dict(sys.modules, {"openai": mock_openai}):
        yield mock_openai


@pytest.fixture
def simple_tools() -> list[ToolSchema]:
    """Create simple tool schemas for testing."""
    return [
        ToolSchema(
            name="search",
            description="Search the web",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        ),
    ]


class TestOpenAIExecutorInit:
    """Tests for OpenAIExecutor initialization."""

    def test_init_with_api_key(self, mock_openai_module) -> None:
        """OpenAIExecutor initializes with API key."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(api_key="sk-test-123")
        assert executor.api_key == "sk-test-123"
        assert executor.default_model == "gpt-4o"

    def test_init_default_model(self, mock_openai_module) -> None:
        """OpenAIExecutor uses custom default model."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(default_model="gpt-4.1")
        assert executor.default_model == "gpt-4.1"

    def test_init_with_api_base(self, mock_openai_module) -> None:
        """OpenAIExecutor accepts custom API base."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(api_base="https://custom.api.com/v1")
        assert executor.api_base == "https://custom.api.com/v1"

    def test_provider_name(self, mock_openai_module) -> None:
        """Provider name is 'openai'."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor()
        assert executor.provider_name == "openai"


class TestOpenAIExecutorClient:
    """Tests for lazy client initialization."""

    def test_lazy_client_with_key(self, mock_openai_module) -> None:
        """Client is lazily created with API key."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(api_key="sk-test")
        assert executor._client is None

        client = executor._get_client()
        mock_openai_module.AsyncOpenAI.assert_called_once_with(api_key="sk-test")
        assert client is not None

    def test_lazy_client_with_base_url(self, mock_openai_module) -> None:
        """Client is created with custom base URL."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(api_key="sk-test", api_base="https://custom.api.com")
        executor._get_client()
        mock_openai_module.AsyncOpenAI.assert_called_once_with(
            api_key="sk-test", base_url="https://custom.api.com"
        )

    def test_lazy_client_no_key(self, mock_openai_module) -> None:
        """Client created without explicit key (reads from env)."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor()
        executor._get_client()
        mock_openai_module.AsyncOpenAI.assert_called_once_with()

    def test_client_cached(self, mock_openai_module) -> None:
        """Client is created only once."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor(api_key="sk-test")
        c1 = executor._get_client()
        c2 = executor._get_client()
        assert c1 is c2
        assert mock_openai_module.AsyncOpenAI.call_count == 1


class TestOpenAIExecutorToolConversion:
    """Tests for tool schema conversion."""

    def test_convert_tools(self, mock_openai_module, simple_tools) -> None:
        """Tools are converted to OpenAI function format."""
        from gobby.llm.openai_executor import OpenAIExecutor

        executor = OpenAIExecutor()
        result = executor._convert_tools(simple_tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
        assert result[0]["function"]["description"] == "Search the web"
        assert "query" in result[0]["function"]["parameters"]["properties"]

    def test_convert_tools_adds_type_if_missing(self, mock_openai_module) -> None:
        """Tool conversion adds type=object if not in schema."""
        from gobby.llm.openai_executor import OpenAIExecutor

        tools = [
            ToolSchema(
                name="test",
                description="A test tool",
                input_schema={"properties": {"x": {"type": "string"}}},
            )
        ]

        executor = OpenAIExecutor()
        result = executor._convert_tools(tools)
        assert result[0]["function"]["parameters"]["type"] == "object"


class TestOpenAIExecutorRun:
    """Tests for the agentic loop."""

    @pytest.fixture
    def executor(self, mock_openai_module):
        """Create an OpenAIExecutor with mocked client."""
        from gobby.llm.openai_executor import OpenAIExecutor

        return OpenAIExecutor(api_key="sk-test")

    def _make_text_response(
        self, text: str, prompt_tokens: int = 10, completion_tokens: int = 5
    ) -> MagicMock:
        """Helper to build a mock text-only response."""
        message = MagicMock()
        message.content = text
        message.tool_calls = None
        message.model_dump = MagicMock(
            return_value={"role": "assistant", "content": text, "tool_calls": None}
        )

        choice = MagicMock()
        choice.message = message

        usage = MagicMock()
        usage.prompt_tokens = prompt_tokens
        usage.completion_tokens = completion_tokens

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage
        return response

    def _make_tool_call_response(
        self,
        tool_name: str,
        args_json: str,
        tool_call_id: str = "call_123",
        prompt_tokens: int = 15,
        completion_tokens: int = 8,
    ) -> MagicMock:
        """Helper to build a mock tool call response."""
        tc = MagicMock()
        tc.id = tool_call_id
        tc.function.name = tool_name
        tc.function.arguments = args_json

        message = MagicMock()
        message.content = None
        message.tool_calls = [tc]
        message.model_dump = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": tool_call_id, "function": {"name": tool_name, "arguments": args_json}}
                ],
            }
        )

        choice = MagicMock()
        choice.message = message

        usage = MagicMock()
        usage.prompt_tokens = prompt_tokens
        usage.completion_tokens = completion_tokens

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage
        return response

    @pytest.mark.asyncio
    async def test_run_simple_response(self, executor, simple_tools) -> None:
        """Run returns text when no tool calls."""
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=self._make_text_response("Hello!")
        )
        executor._client = mock_client

        async def dummy_handler(name, args):
            return ToolResult(tool_name=name, success=True)

        result = await executor.run(prompt="Hi", tools=simple_tools, tool_handler=dummy_handler)

        assert result.status == "success"
        assert result.output == "Hello!"
        assert result.turns_used == 1
        assert result.cost_info is not None
        assert result.cost_info.prompt_tokens == 10

    @pytest.mark.asyncio
    async def test_run_with_tool_call(self, executor, simple_tools) -> None:
        """Run handles tool calls and sends results back."""
        tc_resp = self._make_tool_call_response("search", '{"query": "gobby"}')
        text_resp = self._make_text_response("Found results for gobby.", 20, 10)

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=[tc_resp, text_resp])
        executor._client = mock_client

        async def handler(name, args):
            return ToolResult(tool_name=name, success=True, result={"hits": 5})

        result = await executor.run(prompt="Search gobby", tools=simple_tools, tool_handler=handler)

        assert result.status == "success"
        assert result.output == "Found results for gobby."
        assert result.turns_used == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "search"
        assert result.cost_info is not None
        assert result.cost_info.prompt_tokens == 35

    @pytest.mark.asyncio
    async def test_run_api_error(self, executor, simple_tools) -> None:
        """Run returns error on API failure."""
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Rate limited"))
        executor._client = mock_client

        async def dummy_handler(name, args):
            return ToolResult(tool_name=name, success=True)

        result = await executor.run(prompt="Hi", tools=simple_tools, tool_handler=dummy_handler)

        assert result.status == "error"
        assert "Rate limited" in result.error

    @pytest.mark.asyncio
    async def test_run_max_turns(self, executor, simple_tools) -> None:
        """Run returns partial when max turns exhausted."""
        tc_resp = self._make_tool_call_response("search", '{"query": "test"}')

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=tc_resp)
        executor._client = mock_client

        async def handler(name, args):
            return ToolResult(tool_name=name, success=True, result={})

        result = await executor.run(
            prompt="Search",
            tools=simple_tools,
            tool_handler=handler,
            max_turns=2,
        )

        assert result.status == "partial"
        assert result.turns_used == 2
        assert len(result.tool_calls) == 2

    @pytest.mark.asyncio
    async def test_run_tool_handler_error(self, executor, simple_tools) -> None:
        """Run handles tool handler exceptions gracefully."""
        tc_resp = self._make_tool_call_response("search", '{"query": "test"}')
        text_resp = self._make_text_response("Error occurred.")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=[tc_resp, text_resp])
        executor._client = mock_client

        async def failing_handler(name, args):
            raise RuntimeError("Handler crashed")

        result = await executor.run(
            prompt="Search", tools=simple_tools, tool_handler=failing_handler
        )

        assert result.status == "success"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].result is not None
        assert result.tool_calls[0].result.success is False

    @pytest.mark.asyncio
    async def test_run_malformed_json_args(self, executor, simple_tools) -> None:
        """Run handles malformed JSON in tool call arguments."""
        tc_resp = self._make_tool_call_response("search", "not valid json")
        text_resp = self._make_text_response("Done.")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=[tc_resp, text_resp])
        executor._client = mock_client

        async def handler(name, args):
            return ToolResult(tool_name=name, success=True, result={})

        result = await executor.run(prompt="Search", tools=simple_tools, tool_handler=handler)

        assert result.status == "success"
        # Args should fall back to empty dict
        assert result.tool_calls[0].arguments == {}
