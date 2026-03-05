"""Claude streaming logic.

Async generator for streaming Claude Agent SDK responses with MCP tools.
Extracted from ClaudeLLMProvider.stream_with_mcp_tools() as part of the
Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import StreamEvent

import gobby.llm.sdk_compat  # noqa: F401 — monkey-patches parse_message to handle unknown event types (e.g. rate_limit_event) that crash the SDK's async generator; see sdk_compat.py for details
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ToolCallEvent,
    ToolResultEvent,
    resolve_context_window,
)
from gobby.llm.sdk_utils import format_exception_group, parse_server_name, sanitize_error

logger = logging.getLogger(__name__)


async def stream_with_mcp_tools(
    cli_path: str,
    prompt: str,
    allowed_tools: list[str],
    system_prompt: str | None = None,
    model: str | None = None,
    max_turns: int = 10,
) -> AsyncIterator[ChatEvent]:
    """
    Stream generation with MCP tools, yielding events as they occur.

    This is a standalone async generator that handles the Claude Agent SDK
    query loop. The caller is responsible for auth mode checks and CLI path
    verification.

    Args:
        cli_path: Path to the Claude CLI binary.
        prompt: User prompt to process.
        allowed_tools: List of allowed MCP tool patterns.
        system_prompt: Optional system prompt.
        model: Optional model override (default: None, uses SDK default).
        max_turns: Maximum number of agentic turns (default: 10).

    Yields:
        ChatEvent: One of TextChunk, ToolCallEvent, ToolResultEvent, or DoneEvent.
    """
    # Build mcp_servers config - use .mcp.json if gobby tools requested
    mcp_servers_config: dict[str, Any] | str | None = None

    if any("gobby" in t for t in allowed_tools):
        cwd_config = Path.cwd() / ".mcp.json"
        if cwd_config.exists():
            mcp_servers_config = str(cwd_config)
        else:
            gobby_root = Path(__file__).parent.parent.parent.parent
            gobby_config = gobby_root / ".mcp.json"
            if gobby_config.exists():
                mcp_servers_config = str(gobby_config)

    # Configure Claude Agent SDK with MCP tools
    options = ClaudeAgentOptions(
        system_prompt=system_prompt or "You are Gobby, a helpful assistant with access to tools.",
        max_turns=max_turns,
        model=model or "opus",
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        cli_path=cli_path,
        mcp_servers=mcp_servers_config if mcp_servers_config is not None else {},
        # Enable partial messages to get per-API-call usage from
        # message_start stream events (ResultMessage.usage accumulates
        # across all API calls in the agentic loop).
        include_partial_messages=True,
    )

    tool_calls_count = 0
    pending_tool_calls: dict[str, str] = {}  # Map tool_use_id -> tool_name
    needs_spacing_before_text = False  # Track if we need spacing before text
    last_model: str | None = None
    # Track the LAST API call's input usage from message_start stream events.
    # ResultMessage.usage accumulates across all API calls in the agentic loop.
    _last_call_input: dict[str, int] | None = None

    try:
        async for message in query(prompt=prompt, options=options):
            if message is None:
                continue
            if isinstance(message, StreamEvent):
                ev = message.event
                if isinstance(ev, dict) and ev.get("type") == "message_start":
                    msg_body = ev.get("message")
                    if isinstance(msg_body, dict):
                        u = msg_body.get("usage")
                        if isinstance(u, dict):
                            _last_call_input = u
                continue
            if isinstance(message, ResultMessage):
                # Final result - extract metadata
                cost_usd = getattr(message, "total_cost_usd", None)
                duration_ms = getattr(message, "duration_ms", None)
                # Extract token usage from ResultMessage.usage dict
                # (AssistantMessage does NOT carry usage in the SDK)
                _raw_usage = getattr(message, "usage", None)
                usage: dict[str, Any] = _raw_usage if isinstance(_raw_usage, dict) else {}

                # Prefer per-call values from message_start stream events
                # (accurate context size). Fall back to ResultMessage.usage
                # which is accumulated across all API calls in the turn.
                if _last_call_input:
                    uncached_input = _last_call_input.get("input_tokens", 0) or 0
                    cache_read = _last_call_input.get("cache_read_input_tokens", 0) or 0
                    cache_creation = _last_call_input.get("cache_creation_input_tokens", 0) or 0
                else:
                    uncached_input = usage.get("input_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                total_input = uncached_input + cache_read + cache_creation

                output_tokens = usage.get("output_tokens", 0) or 0

                _model_usage = getattr(message, "_model_usage", None)
                context_window = resolve_context_window(last_model, _model_usage)

                logger.info(
                    "DoneEvent: uncached=%d cache_read=%d cache_creation=%d "
                    "total_input=%d output=%d context_window=%s per_call=%s",
                    uncached_input,
                    cache_read,
                    cache_creation,
                    total_input,
                    output_tokens,
                    context_window,
                    _last_call_input is not None,
                )
                yield DoneEvent(
                    tool_calls_count=tool_calls_count,
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                    input_tokens=uncached_input or None,
                    output_tokens=output_tokens or None,
                    cache_read_input_tokens=cache_read or None,
                    cache_creation_input_tokens=cache_creation or None,
                    total_input_tokens=total_input or None,
                    context_window=context_window,
                )

            elif isinstance(message, AssistantMessage):
                last_model = getattr(message, "model", None)
                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Add spacing before text that follows tool calls/results
                        # This ensures proper paragraph separation in the UI
                        text = block.text
                        if needs_spacing_before_text and text:
                            # Ensure we have a proper paragraph break (double newline)
                            # even if the text starts with a single newline
                            text = text.lstrip("\n")
                            if text:
                                text = "\n\n" + text
                        yield TextChunk(content=text)
                        needs_spacing_before_text = False
                    elif isinstance(block, ToolUseBlock):
                        tool_calls_count += 1
                        server_name = parse_server_name(block.name)
                        pending_tool_calls[block.id] = block.name
                        # Set spacing flag eagerly — if the tool is denied
                        # at the permission gate the SDK may not yield a
                        # ToolResultBlock, causing run-on text.
                        needs_spacing_before_text = True
                        yield ToolCallEvent(
                            tool_call_id=block.id,
                            tool_name=block.name,
                            server_name=server_name,
                            arguments=block.input if isinstance(block.input, dict) else {},
                        )

            elif isinstance(message, UserMessage):
                # UserMessage may contain tool results
                if isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            # Determine success based on is_error attribute
                            is_error = getattr(block, "is_error", False)
                            yield ToolResultEvent(
                                tool_call_id=block.tool_use_id,
                                success=not is_error,
                                result=block.content if not is_error else None,
                                error=str(block.content) if is_error else None,
                            )
                            needs_spacing_before_text = True

    except ExceptionGroup as eg:
        yield TextChunk(content=f"Generation failed: {format_exception_group(eg)}")
        yield DoneEvent(tool_calls_count=tool_calls_count)
    except Exception as e:
        logger.error(f"Failed to stream with MCP tools: {e}", exc_info=True)
        yield TextChunk(content=f"Generation failed: {sanitize_error(e)}")
        yield DoneEvent(tool_calls_count=tool_calls_count)
