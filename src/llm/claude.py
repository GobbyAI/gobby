"""
Claude implementation of LLMProvider.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeLLMProvider(LLMProvider):  # type: ignore[misc]
    """
    Claude implementation of LLMProvider using claude_agent_sdk.

    Uses subscription-based authentication through Claude CLI.
    Supports code execution via Claude's sandbox.
    """

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "claude"

    @property
    def supports_code_execution(self) -> bool:
        """Claude supports code execution via sandbox."""
        return True

    def __init__(self, config: DaemonConfig):
        """
        Initialize ClaudeLLMProvider.

        Args:
            config: Client configuration.
        """
        self.config = config
        self.logger = logger
        self._claude_cli_path = self._find_cli_path()

    def _find_cli_path(self) -> str | None:
        """
        Find Claude CLI path.

        DO NOT resolve symlinks - npm manages the symlink atomically during upgrades.
        Resolving causes race conditions when Claude Code is being reinstalled.
        """
        cli_path = shutil.which("claude")

        if cli_path:
            # Validate CLI exists and is executable
            if not os.path.exists(cli_path):
                self.logger.warning(f"Claude CLI not found: {cli_path}")
                return None
            elif not os.access(cli_path, os.X_OK):
                self.logger.warning(f"Claude CLI not executable: {cli_path}")
                return None
            else:
                self.logger.debug(f"Claude CLI found: {cli_path}")
                return cli_path
        else:
            self.logger.warning("Claude CLI not found in PATH - LLM features disabled")
            return None

    def _verify_cli_path(self) -> str | None:
        """
        Verify CLI path is still valid and retry if needed.

        Handles race condition when npm install updates Claude Code during hook execution.
        Uses exponential backoff retry to wait for npm install to complete.

        Returns:
            Valid CLI path if found, None otherwise
        """
        cli_path = self._claude_cli_path

        # Validate cached path still exists
        # Retry with backoff if missing (may be in the middle of npm install)
        if cli_path and not os.path.exists(cli_path):
            self.logger.warning(
                f"Cached CLI path no longer exists (may have been reinstalled): {cli_path}"
            )
            # Try to find CLI again with retry logic for npm install race condition
            max_retries = 3
            retry_delays = [0.5, 1.0, 2.0]  # Exponential backoff

            for attempt, delay in enumerate(retry_delays, 1):
                cli_path = shutil.which("claude")
                if cli_path and os.path.exists(cli_path):
                    self.logger.debug(
                        f"Found Claude CLI at new location after {attempt} attempt(s): {cli_path}"
                    )
                    self._claude_cli_path = cli_path
                    break

                if attempt < max_retries:
                    self.logger.debug(
                        f"Claude CLI not found, waiting {delay}s before retry {attempt + 1}/{max_retries}"
                    )
                    time.sleep(delay)
                else:
                    self.logger.warning(f"Claude CLI not found in PATH after {max_retries} retries")
                    cli_path = None

        return cli_path

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using Claude.
        """
        cli_path = self._verify_cli_path()
        if not cli_path:
            return "Session summary unavailable (Claude CLI not found)"

        # Build formatted context for prompt template
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

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for generate_summary. "
                "Configure 'session_summary.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(**formatted_context)

        # Configure Claude Agent SDK
        options = ClaudeAgentOptions(
            system_prompt="You are a session summary generator. Create comprehensive, actionable summaries.",
            max_turns=1,
            model=self.config.session_summary.model,
            allowed_tools=[],
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
            return await _run_query()
        except Exception as e:
            self.logger.error(f"Failed to generate summary with Claude: {e}")
            return f"Session summary generation failed: {e}"

    async def synthesize_title(
        self, user_prompt: str, prompt_template: str | None = None
    ) -> str | None:
        """
        Synthesize session title using Claude.
        """
        cli_path = self._verify_cli_path()
        if not cli_path:
            return None

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for synthesize_title. "
                "Configure 'title_synthesis.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(user_prompt=user_prompt)

        # Configure Claude Agent SDK
        options = ClaudeAgentOptions(
            system_prompt="You are a session title generator. Create concise, descriptive titles.",
            max_turns=1,
            model=self.config.title_synthesis.model,
            allowed_tools=[],
            permission_mode="default",
            cli_path=cli_path,
        )

        # Run async query
        async def _run_query() -> str:
            title_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            title_text = block.text
            return title_text.strip()

        try:
            # Retry logic for title synthesis
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    return await _run_query()
                except Exception as e:
                    if attempt < max_retries - 1:
                        self.logger.warning(
                            f"Title synthesis failed (attempt {attempt + 1}), retrying: {e}"
                        )
                        await asyncio.sleep(1)
                    else:
                        raise e
            # This should be unreachable, but mypy can't prove it
            return None  # pragma: no cover
        except Exception as e:
            self.logger.error(f"Failed to synthesize title with Claude: {e}")
            return None

    async def execute_code(
        self,
        code: str,
        language: str = "python",
        context: str | None = None,
        timeout: int | None = None,
        prompt_template: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute code using Claude's code execution sandbox.
        """
        cli_path = self._verify_cli_path()
        if not cli_path:
            return {
                "success": False,
                "error": "Claude CLI not found",
                "language": language,
            }

        if language.lower() != "python":
            return {
                "success": False,
                "error": f"Language '{language}' not supported. Only Python is currently supported.",
            }

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for execute_code. "
                "Configure 'code_execution.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(code=code, context=context or "", language=language)

        # Determine timeout
        actual_timeout = (
            timeout if timeout is not None else self.config.code_execution.default_timeout
        )

        # Configure Claude Agent SDK
        code_exec_config = self.config.code_execution
        options = ClaudeAgentOptions(
            system_prompt="You are a code execution assistant. Execute the provided code and return results.",
            max_turns=code_exec_config.max_turns,
            model=code_exec_config.model,
            allowed_tools=["code_execution"],
            permission_mode="default",
            cli_path=cli_path,
        )

        # Track execution time
        start_time = time.time()

        # Run async query
        async def _run_query() -> str:
            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text
            return result_text

        try:
            result_text = await asyncio.wait_for(_run_query(), timeout=actual_timeout)

            execution_time = time.time() - start_time

            return {
                "success": True,
                "result": result_text.strip(),
                "language": language,
                "execution_time": round(execution_time, 2),
                "context": context,
            }

        except TimeoutError:
            return {
                "success": False,
                "error": f"Code execution timed out after {actual_timeout} seconds",
                "error_type": "TimeoutError",
                "timeout": actual_timeout,
            }
        except Exception as e:
            self.logger.error(f"Code execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "language": language,
            }
