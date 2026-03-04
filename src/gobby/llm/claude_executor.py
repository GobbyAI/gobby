"""
Claude implementation of AgentExecutor using the Claude Agent SDK.

Supports both subscription (CLI-based auth) and api_key (direct API key)
authentication modes. Both use claude-agent-sdk — the SDK handles auth
differences internally.
"""

import asyncio
import concurrent.futures
import json
import logging
import shutil
from collections.abc import Callable, Coroutine
from typing import Any, Literal, cast

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

ClaudeAuthMode = Literal["subscription", "api_key"]


class ClaudeExecutor(AgentExecutor):
    """
    Claude implementation of AgentExecutor using claude-agent-sdk.

    Supports both authentication modes:
    - subscription: Uses Claude CLI for Pro/Team subscription auth.
    - api_key: Uses direct API key auth via the SDK.

    Both modes use the same claude-agent-sdk — the SDK handles auth
    differences internally.

    Example:
        >>> executor = ClaudeExecutor(auth_mode="subscription")
        >>> executor = ClaudeExecutor(auth_mode="api_key", api_key="sk-ant-...")
    """

    _cli_path: str

    def __init__(
        self,
        auth_mode: ClaudeAuthMode = "subscription",
        default_model: str = "opus",
        api_key: str | None = None,
    ):
        """
        Initialize ClaudeExecutor.

        Args:
            auth_mode: "subscription" for CLI auth, "api_key" for direct API key.
            default_model: Default model to use if not specified in run().
            api_key: Anthropic API key (required for api_key mode).

        Raises:
            ValueError: If subscription mode and Claude CLI not found,
                       or api_key mode and no key provided.
        """
        if auth_mode not in ("subscription", "api_key"):
            raise ValueError(
                f"Unsupported auth_mode '{auth_mode}'. Use 'subscription' or 'api_key'."
            )

        self.auth_mode = auth_mode
        self.default_model = default_model
        self.api_key = api_key
        self.logger = logger
        self._cli_path = ""

        if auth_mode == "subscription":
            cli_path = shutil.which("claude")
            if not cli_path:
                raise ValueError(
                    "Claude CLI not found in PATH. Install Claude Code for subscription mode."
                )
            self._cli_path = cli_path
        elif auth_mode == "api_key" and not api_key:
            raise ValueError("api_key is required for api_key auth mode.")

    @property
    def provider_name(self) -> str:
        """Return the provider name."""
        return "claude"

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
        """
        Execute an agentic loop with tool calling via Claude Agent SDK.

        Runs Claude with the given prompt using subscription-based authentication,
        calling tools via tool_handler until completion, max_turns, or timeout.

        Args:
            prompt: The user prompt to process.
            tools: List of available tools with their schemas.
            tool_handler: Callback to execute tool calls.
            system_prompt: Optional system prompt.
            model: Optional model override.
            max_turns: Maximum turns before stopping (default: 10).
            timeout: Maximum execution time in seconds (default: 120.0).

        Returns:
            AgentResult with output, status, and tool call records.
        """
        return await self._run_with_sdk(
            prompt=prompt,
            tools=tools,
            tool_handler=tool_handler,
            system_prompt=system_prompt,
            model=model or self.default_model,
            max_turns=max_turns,
            timeout=timeout,
        )

    async def _run_with_sdk(
        self,
        prompt: str,
        tools: list[ToolSchema],
        tool_handler: ToolHandler,
        system_prompt: str | None,
        model: str,
        max_turns: int,
        timeout: float,
    ) -> AgentResult:
        """
        Run using Claude Agent SDK.

        Handles both subscription (CLI auth) and api_key (direct key) modes.
        The SDK manages auth differences internally.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            create_sdk_mcp_server,
            query,
        )

        tool_calls: list[ToolCallRecord] = []

        # Create in-process tool functions that call our handler
        # The SDK expects sync functions, so we'll use a wrapper
        def make_tool_func(tool_schema: ToolSchema) -> Callable[..., str]:
            """Create a tool function that calls our async handler."""

            def tool_func(**kwargs: Any) -> str:
                # Run the async handler - need to handle already-running loop
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None:
                    # We're in an async context, use run_coroutine_threadsafe
                    coro = cast(
                        Coroutine[Any, Any, ToolResult],
                        tool_handler(tool_schema.name, kwargs),
                    )
                    future: concurrent.futures.Future[ToolResult] = (
                        asyncio.run_coroutine_threadsafe(coro, loop)
                    )
                    try:
                        result = future.result(timeout=30)
                    except concurrent.futures.TimeoutError:
                        return json.dumps({"error": "Tool execution timed out"})
                    except Exception as e:
                        return json.dumps({"error": str(e)})
                else:
                    # No running loop, use asyncio.run
                    coro = cast(
                        Coroutine[Any, Any, ToolResult],
                        tool_handler(tool_schema.name, kwargs),
                    )
                    result = asyncio.run(coro)

                # Record the call
                record = ToolCallRecord(
                    tool_name=tool_schema.name,
                    arguments=kwargs,
                    result=result,
                )
                tool_calls.append(record)

                if result.success:
                    return json.dumps(result.result) if result.result else "Success"
                else:
                    return json.dumps({"error": result.error})

            # Set function metadata for the SDK
            tool_func.__name__ = tool_schema.name
            tool_func.__doc__ = tool_schema.description
            return tool_func

        # Build tool functions
        tool_functions = [make_tool_func(t) for t in tools]

        # Create MCP server config with our tools
        mcp_server = create_sdk_mcp_server(
            name="gobby-executor",
            tools=cast(Any, tool_functions),
        )
        mcp_servers: dict[str, Any] = {"gobby-executor": mcp_server}

        # Build allowed tools list
        allowed_tools = [f"mcp__gobby-executor__{t.name}" for t in tools]

        # Configure SDK options — auth mode determines CLI path vs API key
        sdk_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt or "You are a helpful assistant.",
            "max_turns": max_turns,
            "model": model,
            "allowed_tools": allowed_tools,
            "permission_mode": "bypassPermissions",
            "mcp_servers": mcp_servers,
        }

        if self.auth_mode == "subscription":
            sdk_kwargs["cli_path"] = self._cli_path
        else:
            # api_key mode — pass key directly to SDK
            sdk_kwargs["api_key"] = self.api_key

        options = ClaudeAgentOptions(**sdk_kwargs)

        # Track turns in outer scope so timeout handler can access the count
        turns_counter = [0]

        async def _run_query() -> AgentResult:
            result_text = ""
            turns_used = 0
            result_msg: ResultMessage | None = None

            try:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        result_msg = message
                        if message.result:
                            result_text = message.result
                    elif isinstance(message, AssistantMessage):
                        turns_used += 1
                        turns_counter[0] = turns_used
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                result_text = block.text
                            elif isinstance(block, ToolUseBlock):
                                self.logger.debug(
                                    f"ToolUseBlock: {block.name}, input={block.input}"
                                )
                    elif isinstance(message, UserMessage):
                        if isinstance(message.content, list):
                            for block in message.content:
                                if isinstance(block, ToolResultBlock):
                                    self.logger.debug(f"ToolResultBlock: {block.tool_use_id}")

                # Build cost info for api_key mode
                cost_info: CostInfo | None = None
                if self.auth_mode == "api_key":
                    # Extract token counts from the last ResultMessage
                    prompt_tokens = 0
                    completion_tokens = 0
                    total_cost = 0.0
                    # Walk messages to find the ResultMessage with usage
                    # (result_msg captured during iteration above)
                    if result_msg and result_msg.usage:
                        prompt_tokens = result_msg.usage.get("input_tokens", 0)
                        completion_tokens = result_msg.usage.get("output_tokens", 0)
                    if result_msg and result_msg.total_cost_usd is not None:
                        total_cost = result_msg.total_cost_usd
                    cost_info = CostInfo(
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_cost=total_cost,
                    )

                return AgentResult(
                    output=result_text,
                    status="success",
                    tool_calls=tool_calls,
                    turns_used=turns_used,
                    cost_info=cost_info,
                )

            except Exception as e:
                self.logger.error(f"SDK execution failed: {e}", exc_info=True)
                return AgentResult(
                    output="",
                    status="error",
                    tool_calls=tool_calls,
                    error=str(e),
                    turns_used=0,
                )

        # Run with timeout
        try:
            return await asyncio.wait_for(_run_query(), timeout=timeout)
        except TimeoutError:
            return AgentResult(
                output="",
                status="timeout",
                tool_calls=tool_calls,
                error=f"Execution timed out after {timeout}s",
                turns_used=turns_counter[0],
            )
