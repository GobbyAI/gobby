"""
OpenAI implementation of AgentExecutor using the OpenAI Python SDK.

Direct API calls via AsyncOpenAI — no LiteLLM middleman. Used for
codex/openai provider with api_key auth mode.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from gobby.llm.cost_table import calculate_cost
from gobby.llm.executor import (
    AgentExecutor,
    AgentResult,
    CostInfo,
    ToolCallRecord,
    ToolHandler,
    ToolResult,
    ToolSchema,
)

logger = logging.getLogger(__name__)


class OpenAIExecutor(AgentExecutor):
    """
    OpenAI implementation of AgentExecutor using the openai SDK.

    Uses AsyncOpenAI.chat.completions.create() with tools for function
    calling. Structurally similar to LiteLLMExecutor's agentic loop but
    calls the OpenAI SDK directly.

    The executor implements a proper agentic loop:
    1. Send prompt with tool definitions via Chat Completions API
    2. When model returns tool_calls, execute each via tool_handler
    3. Send tool results back as role="tool" messages
    4. Repeat until model stops requesting tools or limits are reached
    """

    def __init__(
        self,
        default_model: str = "gpt-4o",
        api_key: str | None = None,
        api_base: str | None = None,
    ):
        """
        Initialize OpenAIExecutor.

        Args:
            default_model: Default model name.
            api_key: OpenAI API key. If None, SDK reads from OPENAI_API_KEY env var.
            api_base: Optional custom API base URL (e.g., for Azure or compatible APIs).
        """
        self.default_model = default_model
        self.api_key = api_key
        self.api_base = api_base
        self.logger = logger
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> "AsyncOpenAI":
        """Lazily initialize the AsyncOpenAI client."""
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.api_base is not None:
                kwargs["base_url"] = self.api_base

            self._client = AsyncOpenAI(**kwargs)
        return self._client

    @property
    def provider_name(self) -> str:
        """Return the provider name."""
        return "openai"

    def _convert_tools(self, tools: list[ToolSchema]) -> list[dict[str, Any]]:
        """Convert ToolSchema list to OpenAI function calling format."""
        openai_tools = []
        for tool in tools:
            params = tool.input_schema.copy()
            if "type" not in params:
                params["type"] = "object"

            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": params,
                    },
                }
            )
        return openai_tools

    async def run(
        self,
        prompt: str,
        tools: list[ToolSchema],
        tool_handler: ToolHandler,
        system_prompt: str | None = None,
        model: str | None = None,
        max_turns: int = 10,
        timeout: float = 120.0,
    ) -> AgentResult:
        """Execute an agentic loop with OpenAI function calling."""
        effective_model = model or self.default_model
        tool_calls_records: list[ToolCallRecord] = []
        cost_tracker = CostInfo(model=effective_model)
        turns_counter = [0]

        async def _run_loop() -> AgentResult:
            client = self._get_client()
            turns_used = 0
            final_output = ""

            # Convert tools to OpenAI format
            openai_tools = self._convert_tools(tools) if tools else None

            # Build initial messages
            messages: list[dict[str, Any]] = []
            messages.append(
                {
                    "role": "system",
                    "content": system_prompt or "You are a helpful assistant.",
                }
            )
            messages.append({"role": "user", "content": prompt})

            while turns_used < max_turns:
                turns_used += 1
                turns_counter[0] = turns_used

                try:
                    kwargs: dict[str, Any] = {
                        "model": effective_model,
                        "messages": messages,
                    }
                    if openai_tools:
                        kwargs["tools"] = openai_tools
                        kwargs["tool_choice"] = "auto"

                    response = await client.chat.completions.create(**kwargs)
                except Exception as e:
                    self.logger.error(f"OpenAI API error: {e}")
                    return AgentResult(
                        output="",
                        status="error",
                        tool_calls=tool_calls_records,
                        error=f"OpenAI API error: {e}",
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )

                # Track token usage
                if hasattr(response, "usage") and response.usage:
                    cost_tracker.prompt_tokens += response.usage.prompt_tokens or 0
                    cost_tracker.completion_tokens += response.usage.completion_tokens or 0

                # Process response
                if not response.choices:
                    self.logger.error("OpenAI returned empty choices for model %s", effective_model)
                    return AgentResult(
                        output=final_output,
                        status="error",
                        tool_calls=tool_calls_records,
                        error="OpenAI returned empty choices",
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )
                choice = response.choices[0]
                message = choice.message
                tool_calls = getattr(message, "tool_calls", None)

                # Extract text content
                if message.content:
                    final_output = message.content

                # If no tool calls, we're done
                if not tool_calls:
                    cost_tracker.total_cost = calculate_cost(
                        effective_model,
                        cost_tracker.prompt_tokens,
                        cost_tracker.completion_tokens,
                    )
                    return AgentResult(
                        output=final_output,
                        status="success",
                        tool_calls=tool_calls_records,
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )

                # Add assistant message to history
                messages.append(message.model_dump())

                # Handle tool calls
                for tool_call in tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        self.logger.warning(
                            "Malformed tool call arguments for %s: %s",
                            tool_call.function.name,
                            tool_call.function.arguments,
                        )
                        fn_args = {}

                    # Record the call
                    record = ToolCallRecord(tool_name=fn_name, arguments=fn_args)
                    tool_calls_records.append(record)

                    # Execute via handler
                    try:
                        result = await tool_handler(fn_name, fn_args)
                        record.result = result

                        if result.success:
                            content = (
                                json.dumps(result.result)
                                if result.result is not None
                                else "Success"
                            )
                        else:
                            content = f"Error: {result.error}"
                    except Exception as e:
                        self.logger.error(f"Tool handler error for {fn_name}: {e}")
                        record.result = ToolResult(tool_name=fn_name, success=False, error=str(e))
                        content = f"Error: {e}"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": fn_name,
                            "content": content,
                        }
                    )

            # Max turns reached
            cost_tracker.total_cost = calculate_cost(
                effective_model,
                cost_tracker.prompt_tokens,
                cost_tracker.completion_tokens,
            )
            return AgentResult(
                output=final_output,
                status="partial",
                tool_calls=tool_calls_records,
                turns_used=turns_used,
                cost_info=cost_tracker,
            )

        # Run with timeout
        try:
            return await asyncio.wait_for(_run_loop(), timeout=timeout)
        except TimeoutError:
            cost_tracker.total_cost = calculate_cost(
                effective_model,
                cost_tracker.prompt_tokens,
                cost_tracker.completion_tokens,
            )
            return AgentResult(
                output="",
                status="timeout",
                tool_calls=tool_calls_records,
                error=f"Execution timed out after {timeout}s",
                turns_used=turns_counter[0],
                cost_info=cost_tracker,
            )
