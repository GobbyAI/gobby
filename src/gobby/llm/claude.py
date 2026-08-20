"""Claude LLM provider facade."""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from gobby.config.app import DaemonConfig
from gobby.llm.base import AuthMode, LLMTextResult
from gobby.llm.claude_models import AgenticGenerationResult
from gobby.llm.claude_sdk import ClaudeSDKClient

_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

logger = logging.getLogger(__name__)


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
        """Initialize ClaudeLLMProvider."""
        self.config = config
        self.logger = logger
        self._claude_cli_path = self._find_cli_path()
        self._cli_path_lock = asyncio.Lock()
        self._default_model = _DEFAULT_CLAUDE_MODEL
        self._sdk_client = ClaudeSDKClient(
            default_model=self._default_model,
            verify_cli_path=self._verify_cli_path,
            logger=self.logger,
        )

    def _find_cli_path(self) -> str | None:
        """Find Claude CLI path. Delegates to claude_cli.find_cli_path()."""
        from gobby.llm.claude_cli import find_cli_path

        return find_cli_path()

    async def _verify_cli_path(self) -> str | None:
        """Verify CLI path is still valid. Delegates to claude_cli.verify_cli_path()."""
        from gobby.llm.claude_cli import verify_cli_path

        async with self._cli_path_lock:
            cli_path = await verify_cli_path(self._claude_cli_path)
            self._claude_cli_path = cli_path
            return cli_path

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
        """Generate text using Claude."""
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
        images: Sequence[str] | None = None,
    ) -> LLMTextResult:
        """Generate text and surface Anthropic token usage when available."""
        return await self._sdk_client.generate_text_result(
            prompt,
            system_prompt,
            model,
            max_tokens,
            reasoning_effort=reasoning_effort,
            caller=caller,
            images=images,
        )

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
        """Run a tool-enabled agentic investigation and return a grounded narrative."""
        return await self._sdk_client.generate_agentic(
            system_prompt=system_prompt,
            prompt=prompt,
            project_path=project_path,
            model=model,
            max_turns=max_turns,
            reasoning_effort=reasoning_effort,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            mcp_servers=mcp_servers,
            caller=caller,
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
        """Generate structured JSON using Claude Agent SDK."""
        return await self._sdk_client.generate_json(
            prompt,
            system_prompt,
            model,
            json_schema=json_schema,
            reasoning_effort=reasoning_effort,
            caller=caller,
        )

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        """Generate a text description of an image using Claude vision."""
        return await self._sdk_client.describe_image(image_path, context, model)
