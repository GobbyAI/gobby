"""Claude Agent SDK client flows."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from gobby.llm.base import (
    LLMProviderError,
    LLMTextResult,
)
from gobby.llm.claude_models import AgenticGenerationResult
from gobby.llm.claude_payloads import (
    claude_reasoning_options,
    normalize_claude_usage,
    strip_leading_preamble,
)
from gobby.llm.claude_runtime import (
    execute_sdk_query,
    is_max_turns_error,
    raise_for_error_result,
)
from gobby.llm.image_payloads import prepare_image_inputs
from gobby.llm.textgen_cwd import fixed_textgen_cwd

_FEATURE_TEXTGEN_MAX_TURNS = 8


def _sdk_image_block(mime_type: str, image_base64: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": image_base64,
        },
    }


# One-shot generation needs no memory and must not litter ~/.claude/projects
# with per-run auto-memory state; the SDK merges this over the inherited env.
_TEXTGEN_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


class ClaudeSDKClient:
    """Owns Claude Agent SDK text, agentic, JSON, and vision flows."""

    def __init__(
        self,
        default_model: str,
        verify_cli_path: Callable[[], Awaitable[str | None]],
        logger: logging.Logger,
    ) -> None:
        self._default_model = default_model
        self._verify_cli_path = verify_cli_path
        self.logger = logger

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
        images: Sequence[str] | None = None,
    ) -> LLMTextResult:
        """Generate text using Claude Agent SDK."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        image_content: list[dict[str, Any]] | None = None
        if images:
            image_content = await self._image_message_content(prompt, images)

        with fixed_textgen_cwd() as neutral_cwd:
            reasoning_options = claude_reasoning_options(reasoning_effort)
            applied_reasoning_effort = reasoning_options.get("effort")
            options = ClaudeAgentOptions(
                system_prompt=system_prompt or "You are a helpful assistant.",
                max_turns=_FEATURE_TEXTGEN_MAX_TURNS,
                model=model or self._default_model,
                tools=[],
                allowed_tools=[],
                mcp_servers={},
                permission_mode="default",
                cli_path=cli_path,
                cwd=str(neutral_cwd),
                env=dict(_TEXTGEN_ENV),
                **reasoning_options,
            )

            captured_usage: dict[str, int] | None = None
            operation = f"generate_text[{caller}]" if caller else "generate_text"

            async def _run_query() -> str:
                nonlocal captured_usage
                result_text = ""
                message_count = 0
                attempt_usage: dict[str, int] | None = None
                rate_limit_info: Any | None = None
                query_prompt: str | AsyncIterator[dict[str, Any]] = prompt
                if image_content is not None:
                    content = image_content

                    async def _message_generator() -> AsyncIterator[dict[str, Any]]:
                        yield {
                            "type": "user",
                            "message": {"role": "user", "content": content},
                        }

                    query_prompt = _message_generator()
                async for message in query(prompt=query_prompt, options=options):
                    message_count += 1
                    self.logger.debug(
                        "generate_text message %d: %s",
                        message_count,
                        type(message).__name__,
                    )
                    event_rate_limit = getattr(message, "rate_limit_info", None)
                    if event_rate_limit is not None:
                        rate_limit_info = event_rate_limit
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                self.logger.debug("  TextBlock: %s...", block.text[:100])
                                result_text += block.text
                            elif isinstance(block, ToolUseBlock):
                                self.logger.debug("  ToolUseBlock: %s", block.name)
                    elif isinstance(message, ResultMessage):
                        self.logger.debug(
                            "  ResultMessage: result=%s, type=%s",
                            message.result,
                            type(message.result),
                        )
                        raise_for_error_result(message, operation, rate_limit_info=rate_limit_info)
                        if message.result:
                            result_text = message.result
                        usage = normalize_claude_usage(getattr(message, "usage", None))
                        if usage is not None:
                            attempt_usage = usage
                if message_count == 0:
                    self.logger.warning("generate_text: No messages received from Claude SDK")
                elif not result_text:
                    self.logger.warning(
                        "generate_text: %d messages but no text content", message_count
                    )
                captured_usage = attempt_usage
                return result_text

            result: str = await execute_sdk_query(
                operation, _run_query, options, self.logger, max_retries=3
            )
            if not result.strip():
                raise LLMProviderError(f"Claude {operation} returned blank content")

        return LLMTextResult(
            text=result,
            usage=captured_usage,
            applied_reasoning_effort=applied_reasoning_effort,
        )

    async def _image_message_content(
        self, prompt: str, images: Sequence[str]
    ) -> list[dict[str, Any]]:
        prepared = await prepare_image_inputs(images, self.logger)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            _sdk_image_block(mime_type, image_base64)
            for _path, mime_type, image_base64, _data_url in prepared
        )
        return content

    async def generate_agentic(
        self,
        *,
        system_prompt: str | None,
        prompt: str,
        project_path: str,
        model: str | None = None,
        max_turns: int | None = None,
        reasoning_effort: str | None = None,
        allowed_tools: Sequence[str] = ("Read", "Grep", "Glob"),
        disallowed_tools: Sequence[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        caller: str | None = None,
    ) -> AgenticGenerationResult:
        """Run a tool-enabled agentic investigation."""
        if not prompt or not prompt.strip():
            raise ValueError("Agentic generation requires a non-empty prompt")

        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Agentic generation unavailable (Claude CLI not found)")

        reasoning_options = claude_reasoning_options(reasoning_effort)
        applied_reasoning_effort = reasoning_options.get("effort")
        resolved_model = model or self._default_model
        options = ClaudeAgentOptions(
            system_prompt=system_prompt or "You are a helpful assistant.",
            max_turns=max_turns,
            model=resolved_model,
            allowed_tools=list(allowed_tools),
            disallowed_tools=list(disallowed_tools) if disallowed_tools else [],
            mcp_servers=dict(mcp_servers) if mcp_servers else {},
            permission_mode="bypassPermissions",
            setting_sources=[],
            cli_path=cli_path,
            cwd=project_path,
            env=dict(_TEXTGEN_ENV),
            **reasoning_options,
        )

        captured_usage: dict[str, int] | None = None
        tool_breakdown: dict[str, int] = {}
        tool_use_count = 0
        turn_count = 0
        operation = f"generate_agentic[{caller}]" if caller else "generate_agentic"

        async def _run_query() -> str:
            nonlocal captured_usage, tool_breakdown, tool_use_count, turn_count
            result_text = ""
            message_count = 0
            attempt_usage: dict[str, int] | None = None
            attempt_tool_breakdown: dict[str, int] = {}
            attempt_tool_use_count = 0
            attempt_turn_count = 0
            rate_limit_info: Any | None = None
            try:
                async for message in query(prompt=prompt, options=options):
                    message_count += 1
                    event_rate_limit = getattr(message, "rate_limit_info", None)
                    if event_rate_limit is not None:
                        rate_limit_info = event_rate_limit
                    if isinstance(message, AssistantMessage):
                        attempt_turn_count += 1
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                result_text += block.text
                            elif isinstance(block, ToolUseBlock):
                                attempt_tool_use_count += 1
                                attempt_tool_breakdown[block.name] = (
                                    attempt_tool_breakdown.get(block.name, 0) + 1
                                )
                    elif isinstance(message, ResultMessage):
                        raise_for_error_result(message, operation, rate_limit_info=rate_limit_info)
                        if message.result:
                            result_text = message.result
                        usage = normalize_claude_usage(getattr(message, "usage", None))
                        if usage is not None:
                            attempt_usage = usage
            except Exception as exc:  # noqa: BLE001 - re-raised unless max-turns
                if is_max_turns_error(exc) and result_text.strip():
                    captured_usage = attempt_usage
                    tool_breakdown = attempt_tool_breakdown
                    tool_use_count = attempt_tool_use_count
                    turn_count = attempt_turn_count
                    self.logger.info(
                        "generate_agentic reached max_turns=%s; returning accumulated "
                        "text (%d chars, %d turns, %d tool uses)",
                        max_turns,
                        len(result_text),
                        attempt_turn_count,
                        attempt_tool_use_count,
                    )
                    return result_text
                raise
            if message_count == 0:
                self.logger.warning("generate_agentic: No messages received from Claude SDK")
            elif not result_text:
                self.logger.warning(
                    "generate_agentic: %d messages but no text content", message_count
                )
            captured_usage = attempt_usage
            tool_breakdown = attempt_tool_breakdown
            tool_use_count = attempt_tool_use_count
            turn_count = attempt_turn_count
            return result_text

        raw_text: str = await execute_sdk_query(
            operation, _run_query, options, self.logger, max_retries=1
        )
        return AgenticGenerationResult(
            text=strip_leading_preamble(raw_text),
            model=resolved_model,
            tool_use_count=tool_use_count,
            turns=turn_count,
            tools=dict(tool_breakdown),
            usage=captured_usage,
            applied_reasoning_effort=applied_reasoning_effort,
        )

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        json_schema: dict[str, Any],
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """Generate JSON using Claude Agent SDK with output_format constraint."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        with fixed_textgen_cwd() as neutral_cwd:
            reasoning_options = claude_reasoning_options(reasoning_effort)
            applied_reasoning_effort = reasoning_options.get("effort")
            if applied_reasoning_effort is not None:
                self.logger.debug(
                    "generate_json using Claude reasoning_effort=%s",
                    applied_reasoning_effort,
                )
            options = ClaudeAgentOptions(
                system_prompt=system_prompt or "You are a helpful assistant.",
                max_turns=_FEATURE_TEXTGEN_MAX_TURNS,
                model=model or self._default_model,
                tools=[],
                allowed_tools=[],
                mcp_servers={},
                permission_mode="default",
                cli_path=cli_path,
                output_format={"type": "json_schema", "schema": json_schema},
                cwd=str(neutral_cwd),
                env=dict(_TEXTGEN_ENV),
                **reasoning_options,
            )
            operation = f"generate_json[{caller}]" if caller else "generate_json"

            async def _run_query() -> object | None:
                structured_output: object | None = None
                message_count = 0
                rate_limit_info: Any | None = None
                async for message in query(prompt=prompt, options=options):
                    message_count += 1
                    event_rate_limit = getattr(message, "rate_limit_info", None)
                    if event_rate_limit is not None:
                        rate_limit_info = event_rate_limit
                    self.logger.debug(
                        "generate_json message %d: %s",
                        message_count,
                        type(message).__name__,
                    )
                    if isinstance(message, ResultMessage):
                        raise_for_error_result(message, operation, rate_limit_info=rate_limit_info)
                        structured_output = message.structured_output
                if message_count == 0:
                    self.logger.warning("generate_json: No messages received from Claude SDK")
                return structured_output

            result = await execute_sdk_query(
                operation, _run_query, options, self.logger, max_retries=3
            )

        if not isinstance(result, dict):
            raise ValueError("Claude SDK returned no object structured output")
        return result
