"""Claude SDK primitives for feature-routed LLM capabilities."""

import asyncio
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from gobby.agents.provider_capabilities import provider_reasoning_efforts
from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.config.app import DaemonConfig
from gobby.llm.base import AuthMode, LLMProviderCancellation, LLMTextResult
from gobby.llm.textgen_cwd import neutral_textgen_cwd
from gobby.utils.json_helpers import extract_json_from_text

# Headless settings file — zeroes out all hooks so internal LLM calls
# don't trigger session registration or title synthesis cascades.
_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"
_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

logger = logging.getLogger(__name__)


def _claude_reasoning_options(reasoning_effort: str | None) -> dict[str, Any]:
    """Return SDK kwargs for a normalized, Claude-supported reasoning effort."""
    normalized = normalize_reasoning_effort(reasoning_effort)
    if normalized is None:
        return {}
    supported_efforts = provider_reasoning_efforts("claude")
    if normalized not in supported_efforts:
        supported = ", ".join(sorted(supported_efforts))
        raise ValueError(
            f"Unsupported Claude reasoning effort '{normalized}' (expected {supported})"
        )
    return {"effort": normalized}


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` if it is a non-bool integer, else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_claude_usage(usage: Any) -> dict[str, int] | None:
    """Normalize a Claude Agent SDK usage payload into canonical token counts.

    Maps Anthropic ``input_tokens``/``output_tokens`` onto the OpenAI-style
    ``prompt_tokens``/``completion_tokens``/``total_tokens`` shape the rest of
    the daemon expects, preserving the native and cache fields when present.
    Returns ``None`` when no integer token counts are available.
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        data: dict[str, Any] = usage
    elif hasattr(usage, "model_dump"):
        data = usage.model_dump()
    else:
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
        data = {field: getattr(usage, field, None) for field in fields}

    input_tokens = _coerce_int(data.get("input_tokens"))
    output_tokens = _coerce_int(data.get("output_tokens"))
    cache_creation_input_tokens = _coerce_int(data.get("cache_creation_input_tokens"))
    cache_read_input_tokens = _coerce_int(data.get("cache_read_input_tokens"))
    prompt_tokens = _coerce_int(data.get("prompt_tokens"))
    completion_tokens = _coerce_int(data.get("completion_tokens"))
    total_tokens = _coerce_int(data.get("total_tokens"))

    if prompt_tokens is None:
        prompt_tokens = input_tokens
    if completion_tokens is None:
        completion_tokens = output_tokens
    if total_tokens is None:
        token_parts = (
            prompt_tokens,
            completion_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
        if any(value is not None for value in token_parts):
            total_tokens = sum(value or 0 for value in token_parts)

    result: dict[str, int] = {}
    if prompt_tokens is not None:
        result["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        result["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    if cache_creation_input_tokens is not None:
        result["cache_creation_input_tokens"] = cache_creation_input_tokens
    if cache_read_input_tokens is not None:
        result["cache_read_input_tokens"] = cache_read_input_tokens
    return result or None


@dataclass(frozen=True, kw_only=True)
class AgenticGenerationResult:
    """Result of a tool-enabled agentic investigation run.

    ``text`` is the grounded narrative; the remaining fields describe the
    investigation so callers can surface provenance (how many turns the agent
    took, how many tool uses, and a per-tool breakdown).
    """

    text: str
    model: str
    tool_use_count: int = 0
    turns: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    applied_reasoning_effort: str | None = None


def _strip_leading_preamble(text: str) -> str:
    """Drop any non-markdown preamble before the first Markdown heading.

    The agentic model sometimes prefixes prose (for example
    "Now I have the evidence...") before the grounded page. When a line that
    starts with ``"# "`` or ``"## "`` exists, return everything from that
    heading onward; otherwise return the input stripped of surrounding
    whitespace.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            return "\n".join(lines[index:]).strip()
    return text.strip()


class ClaudeSDKProviderFailure(RuntimeError):
    """Typed failure for known Claude SDK/provider degradation paths."""


class ClaudeSDKShutdownCancellation(LLMProviderCancellation):
    """Raised when the Claude SDK child process is terminated during shutdown."""


# Turn budget for the one-shot feature text-generation path (codewiki, synthesis,
# memory.dream, etc.). Must be >1: with max_turns=1 the Claude Agent SDK raises
# "Reached maximum number of turns (1)" on reasoning/continuation-heavy prompts
# instead of returning text (gobby-#17698). Tools are disabled on this path, so
# the model cannot take action-loops; this is bounded headroom, not an agent loop.
_FEATURE_TEXTGEN_MAX_TURNS = 8


class ClaudeLLMProvider:
    """Claude implementation using the Claude Agent SDK."""

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "claude"

    @property
    def auth_mode(self) -> AuthMode:
        """Return Claude authentication mode."""
        return "subscription"

    def __init__(self, config: DaemonConfig):
        """
        Initialize ClaudeLLMProvider.

        Args:
            config: Client configuration.
        """
        self.config = config
        self.logger = logger

        self._claude_cli_path = self._find_cli_path()

        self._default_model = _DEFAULT_CLAUDE_MODEL

    def _find_cli_path(self) -> str | None:
        """Find Claude CLI path. Delegates to claude_cli.find_cli_path()."""
        from gobby.llm.claude_cli import find_cli_path

        return find_cli_path()

    async def _verify_cli_path(self) -> str | None:
        """Verify CLI path is still valid. Delegates to claude_cli.verify_cli_path()."""
        from gobby.llm.claude_cli import verify_cli_path

        cli_path = await verify_cli_path(self._claude_cli_path)
        self._claude_cli_path = cli_path
        return cli_path

    @staticmethod
    def _is_transient_error(e: Exception) -> bool:
        """Classify whether an error is transient (worth retrying).

        Permanent errors (auth failures, invalid requests) are not retried.
        Transient errors (timeouts, rate limits, server errors) are retried.
        """
        if isinstance(e, LLMProviderCancellation):
            return False
        if ClaudeLLMProvider._is_sdk_sigterm_shutdown(e):
            return False
        if ClaudeLLMProvider._is_error_result_success(e):
            return False
        msg = str(e).lower()
        # Permanent error patterns — fail fast
        permanent_patterns = [
            "401",
            "403",
            "invalid_api_key",
            "authentication",
            "unauthorized",
            "invalid request",
            "invalid_request",
            "permission denied",
            "not_found",
            "404",
        ]
        for pattern in permanent_patterns:
            if pattern in msg:
                return False
        return True

    @staticmethod
    def _is_error_result_success(e: BaseException) -> bool:
        """Return whether the SDK surfaced its known error-result-success shape."""
        return "claude code returned an error result: success" in str(e).lower()

    @staticmethod
    def _is_max_turns_error(e: BaseException) -> bool:
        """Return whether the SDK raised because it hit ``max_turns``.

        The SDK raises (e.g. "Reached maximum number of turns (N)") rather than
        returning partial text when the turn budget is exhausted. The message
        may be wrapped, so walk the exception tree.
        """
        for current in ClaudeLLMProvider._iter_exception_tree(e):
            if "maximum number of turns" in str(current).lower():
                return True
        return False

    @staticmethod
    def _is_sdk_sigterm_shutdown(e: BaseException) -> bool:
        """Return whether the Claude SDK/process was terminated by SIGTERM."""
        return ClaudeLLMProvider._extract_exit_code(e) == 143

    @staticmethod
    def _iter_exception_tree(e: BaseException) -> Iterator[BaseException]:
        """Walk exception, causes, contexts, and exception-group children."""
        stack: list[BaseException] = [e]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            yield current
            if current.__cause__ is not None:
                stack.append(current.__cause__)
            if current.__context__ is not None:
                stack.append(current.__context__)
            children = getattr(current, "exceptions", None)
            if isinstance(children, tuple):
                stack.extend(child for child in children if isinstance(child, BaseException))

    @staticmethod
    def _extract_exit_code_from_message(message: str) -> int | None:
        """Parse common SDK/process messages like 'exit code 143'."""
        normalized = message.lower().replace("_", " ").replace("=", " ").replace(":", " ")
        parts = normalized.split()
        for index, part in enumerate(parts[:-2]):
            if part.strip("([{") == "exit" and parts[index + 1] == "code":
                candidate = parts[index + 2].strip(".,;])}")
                try:
                    return int(candidate)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _extract_exit_code(e: BaseException) -> int | None:
        """Walk __cause__ chain to find ProcessError exit code.

        The SDK's ProcessError has an exit_code attribute, but it gets
        wrapped as a plain Exception through the message stream. This
        walks the exception tree defensively and also handles message-only
        wrappers like "exit code 143".
        """
        for current in ClaudeLLMProvider._iter_exception_tree(e):
            exit_code = getattr(current, "exit_code", None)
            if exit_code is not None:
                try:
                    return int(exit_code)
                except (TypeError, ValueError):
                    pass
            parsed = ClaudeLLMProvider._extract_exit_code_from_message(str(current))
            if parsed is not None:
                return parsed
        return None

    async def _retry_async(
        self,
        operation: Any,
        max_retries: int = 3,
        delay: float = 1.0,
        on_retry: Any | None = None,
    ) -> Any:
        """
        Execute an async operation with retry logic and error classification.

        Permanent errors (auth, invalid request) fail immediately.
        Transient errors use exponential backoff with jitter.

        Args:
            operation: Callable that returns an awaitable (coroutine factory).
            max_retries: Maximum number of attempts (default: 3).
            delay: Base delay in seconds between retries (default: 1.0).
            on_retry: Optional callback(attempt: int, error: Exception) called on retry.

        Returns:
            Result of the operation if successful.

        Raises:
            Exception: The last exception if all retries fail, or immediately
                      for permanent errors.
        """
        import random

        for attempt in range(max_retries):
            try:
                return await operation()
            except Exception as e:
                if not self._is_transient_error(e):
                    raise
                if attempt < max_retries - 1:
                    if on_retry:
                        on_retry(attempt, e)
                    # Exponential backoff with jitter
                    backoff = delay * (2**attempt) + random.uniform(0, delay * 0.5)  # nosec B311
                    await asyncio.sleep(backoff)
                else:
                    raise

    async def _execute_sdk_query(
        self,
        operation: str,
        query_fn: Any,
        options: ClaudeAgentOptions,
        *,
        max_retries: int = 1,
        retry_delay: float = 2.0,
    ) -> Any:
        """Execute an SDK query with stderr capture, retry, drain, and error logging.

        This is the single entry point for all Claude SDK query execution.
        It owns the full lifecycle:
        1. Injects stderr callback into options
        2. Runs query_fn with retry logic
        3. On failure: drains stderr, extracts exit code, logs diagnostics
        4. Re-raises as RuntimeError with stderr content

        Args:
            operation: Human-readable name for logging (e.g. "generate_text").
            query_fn: Async callable that runs the SDK query loop.
            options: ClaudeAgentOptions — stderr will be overwritten.
            max_retries: Number of attempts (1 = no retry).
            retry_delay: Base delay between retries in seconds.
        """
        # Suppress hooks for internal LLM calls — prevents session registration
        # cascade and title synthesis loops. SDK 0.1.56+ merges --settings with
        # user/project settings, so we also disable those sources.
        if not options.settings:
            options.settings = str(_HEADLESS_SETTINGS)
        if not options.setting_sources:
            options.setting_sources = []

        stderr_lines: list[str] = []
        options.stderr = lambda line: stderr_lines.append(line)

        def _on_retry(attempt: int, error: Exception) -> None:
            stderr_ctx = f" stderr={stderr_lines}" if stderr_lines else ""
            self.logger.warning(
                f"{operation} failed (attempt {attempt + 1}), retrying: {error}{stderr_ctx}"
            )
            stderr_lines.clear()

        def _shutdown_cancellation(error: BaseException) -> ClaudeSDKShutdownCancellation:
            exit_code = self._extract_exit_code(error) or 143
            stderr_text = "\n".join(stderr_lines)
            message = (
                f"{operation} cancelled: Claude SDK process terminated "
                f"[exit_code={exit_code}]"
                + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
            )
            self.logger.info(message)
            return ClaudeSDKShutdownCancellation(message)

        try:
            return await self._retry_async(
                query_fn, max_retries=max_retries, delay=retry_delay, on_retry=_on_retry
            )
        except ExceptionGroup as e:
            if self._is_sdk_sigterm_shutdown(e):
                raise _shutdown_cancellation(e) from e
            # Let ExceptionGroup propagate for callers that handle it
            raise
        except Exception as e:
            if self._is_sdk_sigterm_shutdown(e):
                raise _shutdown_cancellation(e) from e

            if self._is_error_result_success(e):
                exit_code = self._extract_exit_code(e)
                stderr_text = "\n".join(stderr_lines)
                message = (
                    f"{operation} provider degraded: Claude SDK returned "
                    "error-result-success"
                    + (f" [exit_code={exit_code}]" if exit_code else "")
                    + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
                )
                self.logger.warning(message)
                raise ClaudeSDKProviderFailure(message) from e

            # Give stderr handler task time to drain before logging
            await asyncio.sleep(0.2)
            exit_code = self._extract_exit_code(e)
            stderr_text = "\n".join(stderr_lines)
            self.logger.error(
                f"{operation} failed: {e}"
                + (f" [exit_code={exit_code}]" if exit_code else "")
                + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else " (no stderr captured)"),
                exc_info=True,
            )
            raise RuntimeError(
                f"{operation} failed: {e}"
                + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
            ) from e

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> str:
        """
        Generate text using Claude.

        Uses Claude Agent SDK via CLI.
        """
        return (
            await self.generate_text_result(
                prompt,
                system_prompt=system_prompt,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                caller=caller,
            )
        ).text

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> LLMTextResult:
        """Generate text and surface Anthropic token usage when available."""
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_text_sdk(
                prompt,
                system_prompt,
                model,
                max_tokens,
                reasoning_effort=reasoning_effort,
                caller=caller,
            )
        raise RuntimeError("Generation unavailable (Claude CLI not found)")

    async def _generate_text_sdk(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> LLMTextResult:
        """Generate text using Claude Agent SDK (subscription mode)."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        # Configure Claude Agent SDK
        # Use tools=[] to disable all tools for pure text generation.
        # cwd is a neutral temp dir (never the project dir) so the spawned claude
        # CLI does not load project context/hooks and pay a variable startup tax.
        with neutral_textgen_cwd() as neutral_cwd:
            reasoning_options = _claude_reasoning_options(reasoning_effort)
            applied_reasoning_effort = reasoning_options.get("effort")
            options = ClaudeAgentOptions(
                system_prompt=system_prompt or "You are a helpful assistant.",
                # gobby-#17698: feature text-gen was starved at max_turns=1. The
                # Claude Agent SDK RAISES ("Reached maximum number of turns (1)")
                # rather than returning accumulated text when the turn budget is
                # exhausted, which reasoning/continuation-heavy prompts (codewiki,
                # synthesis, memory.dream) routinely hit — stalling those features.
                # Tools are disabled below, so the model cannot take action-loops;
                # this bounded headroom only lets a single generation complete.
                max_turns=_FEATURE_TEXTGEN_MAX_TURNS,
                model=model or self._default_model,
                tools=[],  # Explicitly disable all tools
                allowed_tools=[],
                mcp_servers={},
                permission_mode="default",
                cli_path=cli_path,
                cwd=str(neutral_cwd),
                **reasoning_options,
            )

            captured_usage: dict[str, int] | None = None

            async def _run_query() -> str:
                nonlocal captured_usage
                result_text = ""
                message_count = 0
                async for message in query(prompt=prompt, options=options):
                    message_count += 1
                    self.logger.debug(
                        f"generate_text message {message_count}: {type(message).__name__}"
                    )
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                self.logger.debug(f"  TextBlock: {block.text[:100]}...")
                                result_text += block.text
                            elif isinstance(block, ToolUseBlock):
                                self.logger.debug(f"  ToolUseBlock: {block.name}")
                    elif isinstance(message, ResultMessage):
                        self.logger.debug(
                            f"  ResultMessage: result={message.result}, type={type(message.result)}"
                        )
                        if message.result:
                            result_text = message.result
                        usage = _normalize_claude_usage(getattr(message, "usage", None))
                        if usage is not None:
                            captured_usage = usage
                if message_count == 0:
                    self.logger.warning("generate_text: No messages received from Claude SDK")
                elif not result_text:
                    self.logger.warning(
                        f"generate_text: {message_count} messages but no text content"
                    )
                return result_text

            operation = f"generate_text[{caller}]" if caller else "generate_text"
            result: str = await self._execute_sdk_query(
                operation, _run_query, options, max_retries=3
            )
        # SDK doesn't support max_tokens directly; post-truncate if needed
        if max_tokens and len(result) > max_tokens * 4:
            result = result[: max_tokens * 4]
        return LLMTextResult(
            text=result,
            usage=captured_usage,
            applied_reasoning_effort=applied_reasoning_effort,
        )

    async def generate_agentic(
        self,
        *,
        system_prompt: str | None,
        prompt: str,
        project_path: str,
        model: str | None = None,
        max_turns: int = 60,
        reasoning_effort: str | None = None,
        allowed_tools: Sequence[str] = ("Read", "Grep", "Glob"),
        disallowed_tools: Sequence[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        caller: str | None = None,
    ) -> AgenticGenerationResult:
        """Run a tool-enabled agentic investigation and return a grounded narrative.

        Unlike :meth:`generate_text` (single-turn, tools disabled, neutral cwd),
        this enables caller-selected investigation tools, points ``cwd`` at the
        project repository, and grants a high ``max_turns`` so the model can
        explore the code before producing a grounded Markdown page with file
        citations. Shell access is opt-in by passing ``Bash`` in
        ``allowed_tools`` and keeping it out of ``disallowed_tools``.

        The SDK *raises* (rather than returns) when it exhausts ``max_turns``;
        that case is caught inside the query loop and the accumulated assistant
        text is returned, so a long investigation that runs out of turns still
        yields a usable page. An empty accumulation re-raises as a failure.
        """
        if not prompt or not prompt.strip():
            raise ValueError("Agentic generation requires a non-empty prompt")

        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Agentic generation unavailable (Claude CLI not found)")

        reasoning_options = _claude_reasoning_options(reasoning_effort)
        applied_reasoning_effort = reasoning_options.get("effort")
        resolved_model = model or self._default_model

        # ``mcp_servers`` registers in-process SDK MCP tools (e.g. a caller's
        # read-only gcode surface); ``allowed_tools`` advertises which tools the
        # agent may use, and ``disallowed_tools`` is the hard deny lever that
        # remains authoritative under ``bypassPermissions`` (e.g. to forbid
        # Bash/Write so a read-only investigation cannot mutate the repo).
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
            **reasoning_options,
        )

        captured_usage: dict[str, int] | None = None
        tool_breakdown: dict[str, int] = {}
        tool_use_count = 0
        turn_count = 0

        async def _run_query() -> str:
            nonlocal captured_usage, tool_use_count, turn_count
            result_text = ""
            message_count = 0
            try:
                async for message in query(prompt=prompt, options=options):
                    message_count += 1
                    if isinstance(message, AssistantMessage):
                        turn_count += 1
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                result_text += block.text
                            elif isinstance(block, ToolUseBlock):
                                tool_use_count += 1
                                tool_breakdown[block.name] = tool_breakdown.get(block.name, 0) + 1
                    elif isinstance(message, ResultMessage):
                        if message.result:
                            result_text = message.result
                        usage = _normalize_claude_usage(getattr(message, "usage", None))
                        if usage is not None:
                            captured_usage = usage
            except Exception as exc:  # noqa: BLE001 - re-raised unless it is max-turns
                if self._is_max_turns_error(exc) and result_text.strip():
                    self.logger.info(
                        "generate_agentic reached max_turns=%s; returning accumulated "
                        "text (%d chars, %d turns, %d tool uses)",
                        max_turns,
                        len(result_text),
                        turn_count,
                        tool_use_count,
                    )
                    return result_text
                raise
            if message_count == 0:
                self.logger.warning("generate_agentic: No messages received from Claude SDK")
            elif not result_text:
                self.logger.warning(
                    "generate_agentic: %d messages but no text content", message_count
                )
            return result_text

        operation = f"generate_agentic[{caller}]" if caller else "generate_agentic"
        raw_text: str = await self._execute_sdk_query(operation, _run_query, options, max_retries=1)
        return AgenticGenerationResult(
            text=_strip_leading_preamble(raw_text),
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
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate structured JSON using Claude Agent SDK with prompt-based JSON instruction.

        Raises:
            RuntimeError: If CLI is unavailable
            ValueError: If response is empty or not valid JSON
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_json_sdk(
                prompt,
                system_prompt,
                model,
                reasoning_effort=reasoning_effort,
                caller=caller,
            )
        raise RuntimeError("Generation unavailable (Claude CLI not found)")

    async def _generate_json_sdk(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """Generate JSON using Claude Agent SDK with output_format constraint."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        # cwd is a neutral temp dir (never the project dir) so the spawned claude
        # CLI does not load project context/hooks and pay a variable startup tax.
        with neutral_textgen_cwd() as neutral_cwd:
            reasoning_options = _claude_reasoning_options(reasoning_effort)
            applied_reasoning_effort = reasoning_options.get("effort")
            if applied_reasoning_effort is not None:
                self.logger.debug(
                    "generate_json using Claude reasoning_effort=%s",
                    applied_reasoning_effort,
                )
            options = ClaudeAgentOptions(
                system_prompt=system_prompt or "You are a helpful assistant.",
                max_turns=1,
                model=model or self._default_model,
                tools=[],
                allowed_tools=[],
                mcp_servers={},
                permission_mode="default",
                cli_path=cli_path,
                output_format={"type": "json_object"},
                cwd=str(neutral_cwd),
                **reasoning_options,
            )

            async def _run_query() -> str:
                result_text = ""
                message_count = 0
                async for message in query(prompt=prompt, options=options):
                    message_count += 1
                    self.logger.debug(
                        "generate_json message %d: %s", message_count, type(message).__name__
                    )
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                result_text += block.text
                    elif isinstance(message, ResultMessage):
                        if message.result:
                            result_text = message.result
                if message_count == 0:
                    self.logger.warning("generate_json: No messages received from Claude SDK")
                elif not result_text:
                    self.logger.warning(
                        "generate_json: %d messages but no text content", message_count
                    )
                return result_text

            operation = f"generate_json[{caller}]" if caller else "generate_json"
            text = await self._execute_sdk_query(operation, _run_query, options, max_retries=3)
        text = str(text).strip()
        self.logger.debug("generate_json raw response (%d chars): %s", len(text), text[:500])
        if not text:
            raise ValueError("Claude SDK returned empty response for JSON generation")

        try:
            result: dict[str, Any] = json.loads(text)
            return result
        except json.JSONDecodeError as e:
            # Fallback: extract JSON from markdown fences or mixed content
            self.logger.debug("Direct JSON parse failed, trying extract_json_from_text fallback")
            extracted = extract_json_from_text(text)
            if extracted:
                try:
                    result = json.loads(extracted)
                    self.logger.debug(
                        "Fallback extracted JSON (%d chars): %s", len(extracted), extracted[:200]
                    )
                    return result
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Failed to parse Claude response as JSON: {text[:200]}") from e

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Generate a text description of an image using Claude's vision capabilities.

        Args:
            image_path: Path to the image file to describe
            context: Optional context to guide the description
            model: Optional model override

        Returns:
            Text description of the image
        """
        return await self._describe_image_sdk(
            image_path,
            context,
            model,
        )

    def _prepare_image_data(self, image_path: str) -> tuple[str, str] | str:
        """
        Validate and prepare image data for API calls.

        Args:
            image_path: Path to the image file.

        Returns:
            Tuple of (image_base64, mime_type) on success, or error string on failure.
        """
        import base64
        import mimetypes
        from pathlib import Path

        # Validate image exists
        path = Path(image_path)
        if not path.exists():
            return f"Image not found: {image_path}"

        # Read and encode image
        try:
            image_data = path.read_bytes()
            image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
        except Exception as e:
            self.logger.error(f"Failed to read image {image_path}: {e}")
            return f"Failed to read image: {e}"

        # Determine media type
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type not in ["image/jpeg", "image/png", "image/gif", "image/webp"]:
            mime_type = "image/png"

        return (image_base64, mime_type)

    async def _describe_image_sdk(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        """Describe image using Claude Agent SDK (subscription mode)."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            return "Image description unavailable (Claude CLI not found)"

        # Prepare image data
        result = self._prepare_image_data(image_path)
        if isinstance(result, str):
            return result
        image_base64, mime_type = result

        # Build prompt with image
        text_prompt = "Please describe this image in detail, focusing on the key visual elements and any text visible."
        if context:
            text_prompt = f"{context}\n\n{text_prompt}"

        # Configure Claude Agent SDK
        options = ClaudeAgentOptions(
            system_prompt="You are a vision assistant that describes images in detail.",
            max_turns=1,
            model=model or self._default_model,
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            permission_mode="default",
            cli_path=cli_path,
        )

        # Build async generator yielding structured message with image content
        # The SDK accepts AsyncIterable[dict] for multimodal input
        async def _message_generator() -> Any:
            # The SDK streaming-input protocol expects each item wrapped as
            # {"type": "user", "message": {"role": ..., "content": ...}} (see
            # claude_agent_sdk.query docstring). Yielding a bare
            # {"role", "content"} dict is silently dropped by the SDK, which is
            # why vision extraction returned empty text for every model.
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text_prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_base64,
                            },
                        },
                    ],
                },
            }

        async def _run_query() -> str:
            result_text = ""
            message_count = 0
            async for message in query(prompt=_message_generator(), options=options):
                message_count += 1
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text = message.result
            if not result_text:
                self.logger.warning(
                    "describe_image: SDK returned no text content (messages=%d)",
                    message_count,
                )
            return result_text

        try:
            return str(await self._execute_sdk_query("describe_image", _run_query, options))
        except RuntimeError as e:
            return f"Image description failed: {e}"
