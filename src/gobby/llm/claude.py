"""
Claude implementation of LLMProvider.

Supports two authentication modes:
- subscription: Uses Claude Agent SDK via Claude CLI (requires CLI installed)
- api_key: Uses LiteLLM with anthropic/ prefix (BYOK, no CLI needed)
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SettingSource,
    TextBlock,
    ToolUseBlock,
    query,
)

from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProvider
from gobby.utils.json_helpers import extract_json_from_text

# Type alias for auth mode
AuthMode = Literal["subscription", "api_key"]

# Headless settings file — zeroes out all hooks so internal LLM calls
# don't trigger session registration or title synthesis cascades.
_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"

logger = logging.getLogger(__name__)


class ClaudeLLMProvider(LLMProvider):
    """
    Claude implementation of LLMProvider.

    Supports two authentication modes:
    - subscription (default): Uses Claude Agent SDK via Claude CLI
    - api_key: Uses LiteLLM with anthropic/ prefix (BYOK, no CLI needed)

    The auth_mode is determined by:
    1. Constructor parameter (highest priority)
    2. Config file: llm_providers.claude.auth_mode
    3. Default: "subscription"
    """

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "claude"

    @property
    def auth_mode(self) -> AuthMode:
        """Return current authentication mode."""
        return self._auth_mode

    def __init__(
        self,
        config: DaemonConfig,
        auth_mode: AuthMode | None = None,
    ):
        """
        Initialize ClaudeLLMProvider.

        Args:
            config: Client configuration.
            auth_mode: Authentication mode override. If None, uses config or default.
        """
        self.config = config
        self.logger = logger

        self._auth_mode: AuthMode = "subscription"
        self._claude_cli_path = self._find_cli_path()

        # Resolve default model from provider config → global config.
        # Pydantic defaults guarantee llm_providers.default_model is never None.
        providers = getattr(config, "llm_providers", None)
        claude_cfg = getattr(providers, "claude", None) if providers else None
        self._default_model: str = str(
            getattr(claude_cfg, "default_model", None)
            or getattr(providers, "default_model", None)
            or config.llm_providers.default_model
        )

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

    def _format_summary_context(self, context: dict[str, Any], prompt_template: str | None) -> str:
        """
        Format context and validate prompt template for summary generation.

        Transforms list/dict values to strings for template substitution
        and validates that a prompt template is provided. Uses Jinja2 for
        rendering templates with {{ variable }} syntax.

        Args:
            context: Raw context dict with transcript_summary, last_messages, etc.
            prompt_template: Template string with Jinja2 placeholders for context values.

        Returns:
            Formatted prompt string ready for LLM consumption.

        Raises:
            ValueError: If prompt_template is None.
        """
        # Transform list/dict values to strings for template substitution
        formatted_context = {
            "transcript_summary": context.get("transcript_summary", ""),
            "last_messages": json.dumps(context.get("last_messages", []), indent=2),
            "git_status": context.get("git_status", ""),
            "file_changes": context.get("file_changes", ""),
            **{
                k: v
                for k, v in context.items()
                if k not in ["transcript_summary", "last_messages", "git_status", "file_changes"]
            },
        }

        # Validate prompt_template is provided
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for generate_summary. "
                "Configure 'session_summary.prompt' via gobby-config MCP tools"
            )

        # Render with Jinja2 (templates use {{ variable }} syntax)
        try:
            from jinja2 import Environment

            env = Environment(autoescape=False)  # nosec B701 # generating text prompts
            template = env.from_string(prompt_template)
            rendered: str = template.render(**formatted_context)
            return rendered
        except ImportError:
            # Fallback to simple str.format if Jinja2 unavailable
            # Convert {{ }} to {} for str.format compatibility
            self.logger.warning("Jinja2 not available, using str.format fallback")
            return prompt_template.format(**formatted_context)

    @staticmethod
    def _is_transient_error(e: Exception) -> bool:
        """Classify whether an error is transient (worth retrying).

        Permanent errors (auth failures, invalid requests) are not retried.
        Transient errors (timeouts, rate limits, server errors) are retried.
        """
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
    def _extract_exit_code(e: BaseException) -> int | None:
        """Walk __cause__ chain to find ProcessError exit code.

        The SDK's ProcessError has an exit_code attribute, but it gets
        wrapped as a plain Exception through the message stream. This
        walks the chain defensively in case the SDK fixes that.
        """
        current: BaseException | None = e
        while current is not None:
            if hasattr(current, "exit_code"):
                return int(current.exit_code)
            current = current.__cause__
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
        # Note: [""] not [] — empty list is falsy, SDK skips the flag entirely.
        # [""] produces --setting-sources "" which CLI parses as no sources.
        # TODO: Remove this workaround when the SDK exposes a flag to disable setting sources.
        if not options.settings:
            options.settings = str(_HEADLESS_SETTINGS)
        if not options.setting_sources:
            options.setting_sources = cast(list[SettingSource], [""])

        stderr_lines: list[str] = []
        options.stderr = lambda line: stderr_lines.append(line)

        def _on_retry(attempt: int, error: Exception) -> None:
            stderr_ctx = f" stderr={stderr_lines}" if stderr_lines else ""
            self.logger.warning(
                f"{operation} failed (attempt {attempt + 1}), retrying: {error}{stderr_ctx}"
            )
            stderr_lines.clear()

        try:
            return await self._retry_async(
                query_fn, max_retries=max_retries, delay=retry_delay, on_retry=_on_retry
            )
        except ExceptionGroup:
            # Let ExceptionGroup propagate for callers that handle it
            raise
        except Exception as e:
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

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using Claude.

        Uses Claude Agent SDK via CLI.
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_summary_sdk(context, prompt_template)
        return "Session summary unavailable (Claude CLI not found)"

    async def _generate_summary_sdk(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """Generate session summary using Claude Agent SDK (subscription mode)."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            return "Session summary unavailable (Claude CLI not found)"

        prompt = self._format_summary_context(context, prompt_template)

        # Configure Claude Agent SDK
        options = ClaudeAgentOptions(
            system_prompt="You are a session summary generator. Create comprehensive, actionable summaries.",
            max_turns=1,
            model=self.config.session_summary.model,
            tools=[],  # Passes --tools "" to CLI, disabling all built-in tools
            allowed_tools=[],  # Intent: no tools. Note: SDK ignores due to falsy [] check
            mcp_servers={},
            permission_mode="default",
            cli_path=cli_path,
        )

        # Run async query
        async def _run_query() -> str:
            summary_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            summary_text += block.text
            return summary_text

        try:
            summary_text = await self._execute_sdk_query("generate_summary", _run_query, options)
            if not summary_text:
                sid = context.get("session_id", "unknown")
                self.logger.warning(
                    f"Claude SDK query returned empty response for summary generation (session {sid})",
                )
            return str(summary_text)
        except RuntimeError as e:
            return f"Session summary generation failed: {e}"

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        Generate text using Claude.

        Uses Claude Agent SDK via CLI.
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_text_sdk(prompt, system_prompt, model, max_tokens)
        raise RuntimeError("Generation unavailable (Claude CLI not found)")

    async def _generate_text_sdk(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate text using Claude Agent SDK (subscription mode)."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        # Configure Claude Agent SDK
        # Use tools=[] to disable all tools for pure text generation
        options = ClaudeAgentOptions(
            system_prompt=system_prompt or "You are a helpful assistant.",
            max_turns=1,
            model=model or self._default_model,
            tools=[],  # Explicitly disable all tools
            allowed_tools=[],
            mcp_servers={},
            permission_mode="default",
            cli_path=cli_path,
        )

        async def _run_query() -> str:
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
            if message_count == 0:
                self.logger.warning("generate_text: No messages received from Claude SDK")
            elif not result_text:
                self.logger.warning(f"generate_text: {message_count} messages but no text content")
            return result_text

        result: str = await self._execute_sdk_query(
            "generate_text", _run_query, options, max_retries=3
        )
        # SDK doesn't support max_tokens directly; post-truncate if needed
        if max_tokens and len(result) > max_tokens * 4:
            result = result[: max_tokens * 4]
        return result

    async def generate_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        allowed_tools: list[str],
        max_turns: int = 3,
        model: str | None = None,
    ) -> str:
        """Generate text using Claude with specific built-in tools enabled.

        For multi-turn SDK calls that need tool use (e.g. WebFetch, WebSearch)
        but should remain invisible to the session/hook system.
        """
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=max_turns,
            model=model or self._default_model,
            allowed_tools=allowed_tools,
            mcp_servers={},
            permission_mode="default",
            cli_path=cli_path,
        )

        async def _run_query() -> str:
            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text
                elif isinstance(message, ResultMessage) and message.result:
                    result_text = message.result
            return result_text

        result: str = await self._execute_sdk_query(
            "generate_with_tools", _run_query, options, max_retries=1
        )
        return result

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate structured JSON using Claude Agent SDK with prompt-based JSON instruction.

        Raises:
            RuntimeError: If CLI is unavailable
            ValueError: If response is empty or not valid JSON
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_json_sdk(prompt, system_prompt, model)
        raise RuntimeError("Generation unavailable (Claude CLI not found)")

    async def _generate_json_sdk(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Generate JSON using Claude Agent SDK with output_format constraint."""
        cli_path = await self._verify_cli_path()
        if not cli_path:
            raise RuntimeError("Generation unavailable (Claude CLI not found)")

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
                self.logger.warning("generate_json: %d messages but no text content", message_count)
            return result_text

        text = await self._execute_sdk_query("generate_json", _run_query, options, max_retries=3)
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
        return await self._describe_image_sdk(image_path, context, model)

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
            yield {
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
            }

        async def _run_query() -> str:
            result_text = ""
            async for message in query(prompt=_message_generator(), options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text = message.result
            return result_text

        try:
            return str(await self._execute_sdk_query("describe_image", _run_query, options))
        except RuntimeError as e:
            return f"Image description failed: {e}"
