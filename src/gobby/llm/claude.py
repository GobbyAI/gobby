"""
Claude implementation of LLMProvider.

Supports two authentication modes:
- subscription: Uses Claude Agent SDK via Claude CLI (requires CLI installed)
- api_key: Uses LiteLLM with anthropic/ prefix (BYOK, no CLI needed)
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

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

from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProvider
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    MCPToolResult,
    TextChunk,
    ToolCall,
)

# Type alias for auth mode
AuthMode = Literal["subscription", "api_key"]


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
        self._litellm: Any = None

        # Determine auth mode from param -> config -> default
        self._auth_mode: AuthMode = "subscription"
        if auth_mode:
            self._auth_mode = auth_mode
        elif config.llm_providers and config.llm_providers.claude:
            config_auth = config.llm_providers.claude.auth_mode
            if config_auth in ("subscription", "api_key"):
                self._auth_mode = cast(AuthMode, config_auth)

        # Set up based on auth mode
        if self._auth_mode == "subscription":
            self._claude_cli_path = self._find_cli_path()
        else:  # api_key
            self._claude_cli_path = None
            self._setup_litellm()

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

    def _setup_litellm(self) -> None:
        """
        Initialize LiteLLM for api_key mode.

        LiteLLM reads ANTHROPIC_API_KEY from the environment automatically.
        """
        try:
            import litellm

            self._litellm = litellm
            self.logger.debug("LiteLLM initialized for Claude api_key mode")
        except ImportError:
            self.logger.error("litellm package required for api_key mode")

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

            env = Environment(autoescape=False)  # nosec B701 - generating text prompts
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

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using Claude.

        Always tries SDK first (works with any auth_mode if CLI is available),
        falls back to LiteLLM only if CLI is unavailable.
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_summary_sdk(context, prompt_template)
        elif self._litellm:
            return await self._generate_summary_litellm(context, prompt_template)
        else:
            return "Session summary unavailable (no LLM backend configured)"

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
            summary_text = await _run_query()
            if not summary_text:
                sid = context.get("session_id", "unknown")
                self.logger.warning(
                    "Claude SDK query returned empty response for summary generation (session %s)",
                    sid,
                )
            return summary_text
        except Exception as e:
            self.logger.error(f"Failed to generate summary with Claude: {e}")
            return f"Session summary generation failed: {e}"

    async def _generate_summary_litellm(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """Generate session summary using LiteLLM (api_key mode)."""
        if not self._litellm:
            return "Session summary unavailable (LiteLLM not initialized)"

        prompt = self._format_summary_context(context, prompt_template)

        try:
            response = await self._litellm.acompletion(
                model=f"anthropic/{self.config.session_summary.model}",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a session summary generator. Create comprehensive, actionable summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
                timeout=120,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self.logger.error(f"Failed to generate summary with LiteLLM: {e}")
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

        Always tries SDK first, falls back to LiteLLM only if CLI is unavailable.
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_text_sdk(prompt, system_prompt, model, max_tokens)
        elif self._litellm:
            return await self._generate_text_litellm(prompt, system_prompt, model, max_tokens)
        else:
            raise RuntimeError("Generation unavailable (no LLM backend configured)")

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
            model=model or "opus",
            tools=[],  # Explicitly disable all tools
            allowed_tools=[],
            mcp_servers={},
            permission_mode="default",
            cli_path=cli_path,
        )
        _max_tokens = max_tokens

        # Run async query
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
                    # ResultMessage contains the final result from the agent
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

        def _on_retry(attempt: int, error: Exception) -> None:
            self.logger.warning(f"generate_text failed (attempt {attempt + 1}), retrying: {error}")

        try:
            result: str = await self._retry_async(
                _run_query, max_retries=3, delay=2.0, on_retry=_on_retry
            )
            # SDK doesn't support max_tokens directly; post-truncate if needed
            if _max_tokens and len(result) > _max_tokens * 4:
                result = result[: _max_tokens * 4]
            return result
        except Exception as e:
            self.logger.error(f"Failed to generate text with Claude: {e}", exc_info=True)
            raise RuntimeError(f"Failed to generate text with Claude: {e}") from e

    async def _generate_text_litellm(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate text using LiteLLM (api_key mode)."""
        if not self._litellm:
            raise RuntimeError("Generation unavailable (LiteLLM not initialized)")

        from gobby.llm.litellm_executor import resolve_model_alias

        model = resolve_model_alias(model or "haiku")
        litellm_model = f"anthropic/{model}"

        try:
            response = await self._litellm.acompletion(
                model=litellm_model,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt or "You are a helpful assistant.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens or 8000,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self.logger.error(f"Failed to generate text with LiteLLM: {e}", exc_info=True)
            raise RuntimeError(f"Failed to generate text with LiteLLM: {e}") from e

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate structured JSON using Claude.

        In api_key mode, uses LiteLLM with response_format.
        In subscription mode, uses SDK with prompt-based JSON instruction.

        Raises:
            RuntimeError: If no LLM backend is available
            ValueError: If response is empty or not valid JSON
        """
        cli_path = await self._verify_cli_path()
        if cli_path:
            return await self._generate_json_sdk(prompt, system_prompt, model)
        elif self._litellm:
            return await self._generate_json_litellm(prompt, system_prompt, model)
        else:
            raise RuntimeError("Generation unavailable (no LLM backend configured)")

    async def _generate_json_sdk(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Generate JSON using Claude Agent SDK (subscription mode)."""
        # SDK doesn't support response_format, so we rely on prompt instructions
        json_system = (system_prompt or "You are a helpful assistant.") + (
            "\n\nIMPORTANT: You MUST respond with valid JSON only. No other text."
        )
        text = await self._generate_text_sdk(prompt, json_system, model)

        # Try to extract JSON from the response
        try:
            # Strip markdown code fences if present
            content = text.strip()
            if content.startswith("```"):
                first_newline = content.find("\n")
                last_fence = content.rfind("```")
                if first_newline != -1 and last_fence > first_newline:
                    content = content[first_newline + 1 : last_fence].strip()
                else:
                    lines = content.split("\n")
                    content = "\n".join(lines[1:-1]).strip()
            result: dict[str, Any] = json.loads(content)
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse LLM response as JSON: {e}") from e

    async def _generate_json_litellm(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Generate JSON using LiteLLM (api_key mode)."""
        if not self._litellm:
            raise RuntimeError("Generation unavailable (LiteLLM not initialized)")

        from gobby.llm.litellm_executor import resolve_model_alias

        model = resolve_model_alias(model or "haiku")
        litellm_model = f"anthropic/{model}"

        try:
            response = await self._litellm.acompletion(
                model=litellm_model,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        or "You are a helpful assistant. Respond with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from LLM")
            result: dict[str, Any] = json.loads(content)
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse LLM response as JSON: {e}") from e

    async def generate_with_mcp_tools(
        self,
        prompt: str,
        allowed_tools: list[str],
        system_prompt: str | None = None,
        model: str | None = None,
        max_turns: int = 10,
        tool_functions: dict[str, list[Any]] | None = None,
    ) -> MCPToolResult:
        """
        Generate text with access to MCP tools.

        This method enables the agent to call MCP tools during generation,
        tracking all tool calls made and returning them alongside the final text.

        Note: This method requires subscription mode (Claude Agent SDK).
        In api_key mode, returns an error message.

        Args:
            prompt: User prompt to process.
            allowed_tools: List of allowed MCP tool patterns.
                Tools should be in format "mcp__{server}__{tool}" or patterns
                like "mcp__gobby-tasks__*" for all tools from a server.
            system_prompt: Optional system prompt.
            model: Optional model override (default: claude-sonnet-4-6).
            max_turns: Maximum number of agentic turns (default: 10).
            tool_functions: Optional dict mapping server names to lists of tool
                functions for in-process MCP servers. Example:
                {"gobby-tasks": [create_task_func, update_task_func]}

        Returns:
            MCPToolResult containing final text and list of tool calls made.

        Example:
            >>> result = await provider.generate_with_mcp_tools(
            ...     prompt="Create a task called 'Fix bug'",
            ...     allowed_tools=["mcp__gobby-tasks__create_task"],
            ...     system_prompt="You are a task manager.",
            ...     tool_functions={"gobby-tasks": [create_task]}
            ... )
            >>> print(result.text)
            >>> for call in result.tool_calls:
            ...     print(f"Called {call.tool_name} with {call.arguments}")
        """
        # MCP tools require subscription mode (Claude Agent SDK)
        if self._auth_mode == "api_key":
            return MCPToolResult(
                text="MCP tools require subscription mode. "
                "Set auth_mode: subscription in llm_providers.claude config.",
                tool_calls=[],
            )

        cli_path = await self._verify_cli_path()
        if not cli_path:
            return MCPToolResult(
                text="Generation unavailable (Claude CLI not found)",
                tool_calls=[],
            )

        # Build mcp_servers config
        # Can be a dict of server configs OR a path to .mcp.json file
        from pathlib import Path

        mcp_servers_config: dict[str, Any] | str | None = None

        # Add in-process tool functions if provided
        if tool_functions:
            mcp_servers_config = {}
            for server_name, tools in tool_functions.items():
                mcp_servers_config[server_name] = create_sdk_mcp_server(
                    name=server_name,
                    tools=tools,
                )

        # If no tool_functions provided but we have allowed gobby tools,
        # use the .mcp.json config file (avoids in-process config issues)
        if not tool_functions and any("gobby" in t for t in allowed_tools):
            # Look for .mcp.json in the current working directory or gobby project
            cwd_config = Path.cwd() / ".mcp.json"
            if cwd_config.exists():
                mcp_servers_config = str(cwd_config)
            else:
                # Try the gobby project root
                gobby_root = Path(__file__).parent.parent.parent.parent
                gobby_config = gobby_root / ".mcp.json"
                if gobby_config.exists():
                    mcp_servers_config = str(gobby_config)

        # Configure Claude Agent SDK with MCP tools
        options = ClaudeAgentOptions(
            system_prompt=system_prompt or "You are a helpful assistant with access to MCP tools.",
            max_turns=max_turns,
            model=model or "opus",
            allowed_tools=allowed_tools,
            permission_mode="bypassPermissions",
            cli_path=cli_path,
            mcp_servers=mcp_servers_config if mcp_servers_config is not None else {},
        )

        # Track tool calls and results
        tool_calls: list[ToolCall] = []
        pending_tool_calls: dict[str, ToolCall] = {}  # Map tool_use_id -> ToolCall

        from gobby.llm.sdk_utils import parse_server_name as _parse_server_name

        # Run async query
        async def _run_query() -> str:
            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    # Final result from the agent
                    if message.result:
                        result_text = message.result
                    self.logger.debug(f"ResultMessage: result={message.result}")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            # Ensure spacing between text from different
                            # AssistantMessages (tool calls in between may
                            # not yield ToolResultBlocks if denied).
                            text = block.text
                            if result_text and text:
                                if not result_text[-1].isspace() and not text[0].isspace():
                                    result_text += "\n\n"
                            result_text += text
                        elif isinstance(block, ToolUseBlock):
                            # Track tool use
                            tool_call = ToolCall(
                                tool_name=block.name,
                                server_name=_parse_server_name(block.name),
                                arguments=block.input if isinstance(block.input, dict) else {},
                            )
                            tool_calls.append(tool_call)
                            pending_tool_calls[block.id] = tool_call
                            self.logger.debug(
                                f"ToolUseBlock: tool={block.name}, input={block.input}"
                            )

                elif isinstance(message, UserMessage):
                    # UserMessage may contain tool results
                    # UserMessage.content can be str | list[...], check first
                    if isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                # Match result to pending tool call
                                if block.tool_use_id in pending_tool_calls:
                                    pending_tool_calls[block.tool_use_id].result = str(
                                        block.content
                                    )
                                self.logger.debug(
                                    f"ToolResultBlock: id={block.tool_use_id}, content={block.content}"
                                )

            return result_text

        try:
            final_text = await _run_query()
            return MCPToolResult(text=final_text, tool_calls=tool_calls)
        except ExceptionGroup as eg:
            # Handle Python 3.11+ ExceptionGroup from TaskGroup
            errors: list[str] = []
            for exc in eg.exceptions:
                errors.append(f"{type(exc).__name__}: {exc}")
                self.logger.error(f"TaskGroup sub-exception: {type(exc).__name__}: {exc}")
            return MCPToolResult(
                text=f"Generation failed: {'; '.join(errors)}",
                tool_calls=tool_calls,
            )
        except Exception as e:
            self.logger.error(f"Failed to generate with MCP tools: {e}", exc_info=True)
            return MCPToolResult(
                text=f"Generation failed: {e}",
                tool_calls=tool_calls,
            )

    async def stream_with_mcp_tools(
        self,
        prompt: str,
        allowed_tools: list[str],
        system_prompt: str | None = None,
        model: str | None = None,
        max_turns: int = 10,
    ) -> AsyncIterator[ChatEvent]:
        """Stream generation with MCP tools. Delegates to claude_streaming module."""
        from gobby.llm.claude_streaming import (
            stream_with_mcp_tools as _stream,
        )

        # MCP tools require subscription mode (Claude Agent SDK)
        if self._auth_mode == "api_key":
            yield TextChunk(
                content="MCP tools require subscription mode. "
                "Set auth_mode: subscription in llm_providers.claude config."
            )
            yield DoneEvent(tool_calls_count=0)
            return

        cli_path = await self._verify_cli_path()
        if not cli_path:
            yield TextChunk(content="Generation unavailable (Claude CLI not found)")
            yield DoneEvent(tool_calls_count=0)
            return

        async for event in _stream(
            cli_path=cli_path,
            prompt=prompt,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            model=model,
            max_turns=max_turns,
        ):
            yield event

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
    ) -> str:
        """
        Generate a text description of an image using Claude's vision capabilities.

        In subscription mode, uses Claude Agent SDK.
        In api_key mode, uses LiteLLM with anthropic/ prefix.

        Args:
            image_path: Path to the image file to describe
            context: Optional context to guide the description

        Returns:
            Text description of the image
        """
        if self._auth_mode == "subscription":
            return await self._describe_image_sdk(image_path, context)
        else:
            return await self._describe_image_litellm(image_path, context)

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
            model="haiku",
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
            return await _run_query()
        except Exception as e:
            self.logger.error(f"Failed to describe image with Claude SDK: {e}")
            return f"Image description failed: {e}"

    async def _describe_image_litellm(
        self,
        image_path: str,
        context: str | None = None,
    ) -> str:
        """Describe image using LiteLLM (api_key mode)."""
        from gobby.llm.litellm_executor import resolve_model_alias

        if not self._litellm:
            return "Image description unavailable (LiteLLM not initialized)"

        # Prepare image data
        result = self._prepare_image_data(image_path)
        if isinstance(result, str):
            return result
        image_base64, mime_type = result

        # Build prompt
        prompt = "Please describe this image in detail, focusing on the key visual elements and any text visible."
        if context:
            prompt = f"{context}\n\n{prompt}"

        try:
            # Route through LiteLLM with anthropic prefix
            # Use same model as SDK path for consistency
            response = await self._litellm.acompletion(
                model=f"anthropic/{resolve_model_alias('haiku')}",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                            },
                        ],
                    }
                ],
                max_tokens=1024,
            )

            if not response or not getattr(response, "choices", None):
                return "No description generated"
            return response.choices[0].message.content or "No description generated"

        except Exception as e:
            self.logger.error(f"Failed to describe image with LiteLLM: {e}")
            return f"Image description failed: {e}"
