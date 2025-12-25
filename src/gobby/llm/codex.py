"""
Codex (OpenAI) implementation of LLMProvider.

Codex CLI supports both subscription-based (ChatGPT) and API key authentication.
After OAuth login, the CLI stores an OpenAI API key in ~/.codex/auth.json,
which can be used with the standard OpenAI Python SDK.

Auth priority:
1. ~/.codex/auth.json (subscription mode)
2. OPENAI_API_KEY environment variable (BYOK mode)
"""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Literal, cast

from gobby.config.app import DaemonConfig
from gobby.llm.base import AuthMode, LLMProvider

logger = logging.getLogger(__name__)


class CodexProvider(LLMProvider):  # type: ignore[misc]
    """
    Codex (OpenAI) implementation of LLMProvider.

    Supports two authentication modes:
    - subscription: Read API key from ~/.codex/auth.json (after `codex login`)
    - api_key: Use OPENAI_API_KEY environment variable (BYOK)

    Code execution uses `codex exec` for sandbox access when available.
    """

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "codex"

    @property
    def auth_mode(self) -> AuthMode:
        """Return the authentication mode for this provider."""
        return self._auth_mode

    @property
    def supports_code_execution(self) -> bool:
        """Codex supports code execution via `codex exec`."""
        return self._codex_cli_available

    def __init__(
        self,
        config: DaemonConfig,
        auth_mode: Literal["subscription", "api_key"] | None = None,
    ):
        """
        Initialize CodexProvider.

        Args:
            config: Client configuration.
            auth_mode: Override auth mode. If None, reads from config.llm_providers.codex.auth_mode
                      or auto-detects based on available credentials.
        """
        self.config = config
        self.logger = logger
        self._client = None
        self._codex_cli_available = False

        # Determine auth mode from config or parameter
        self._auth_mode: AuthMode = "subscription"  # Default
        if auth_mode:
            self._auth_mode = auth_mode
        elif config.llm_providers and config.llm_providers.codex:
            self._auth_mode = config.llm_providers.codex.auth_mode

        # Check if Codex CLI is available (for code execution)
        self._codex_cli_path = shutil.which("codex")
        if self._codex_cli_path:
            self._codex_cli_available = True
            self.logger.debug(f"Codex CLI found at: {self._codex_cli_path}")
        else:
            self.logger.debug("Codex CLI not found - code execution disabled")

        # Get API key based on auth mode
        api_key = self._get_api_key()

        if not api_key:
            self.logger.warning(
                "No Codex API key found. "
                "Run 'codex login' for subscription mode or set OPENAI_API_KEY for BYOK."
            )
            return

        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key)
            self.logger.debug(f"Codex provider initialized (auth_mode: {self._auth_mode})")

        except ImportError:
            self.logger.error("OpenAI package not found. Please install with `pip install openai`.")
        except Exception as e:
            self.logger.error(f"Failed to initialize Codex client: {e}")

    def _get_api_key(self) -> str | None:
        """
        Get API key based on auth mode.

        For subscription mode, reads from ~/.codex/auth.json.
        For api_key mode, reads from OPENAI_API_KEY environment variable.

        Returns:
            API key string or None if not found
        """
        if self._auth_mode == "subscription":
            # Try to read from Codex auth.json
            auth_path = Path.home() / ".codex" / "auth.json"
            if auth_path.exists():
                try:
                    with open(auth_path) as f:
                        auth_data = json.load(f)
                    api_key = auth_data.get("OPENAI_API_KEY")
                    if api_key:
                        self.logger.debug("Loaded API key from ~/.codex/auth.json")
                        return cast(str | None, api_key)
                except Exception as e:
                    self.logger.warning(f"Failed to read ~/.codex/auth.json: {e}")

            # Subscription mode but no auth.json - suggest login
            self.logger.warning(
                "Codex subscription mode but ~/.codex/auth.json not found. "
                "Run 'codex login' to authenticate."
            )
            return None
        else:
            # API key mode - read from environment
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                self.logger.debug("Using OPENAI_API_KEY from environment")
            return api_key

    def _get_model(self, task: str) -> str:
        """
        Get the model to use for a specific task.

        Args:
            task: Task type ("summary" or "title")

        Returns:
            Model name string
        """
        if task == "summary":
            return self.config.session_summary.model or "gpt-4o"
        elif task == "title":
            return self.config.title_synthesis.model or "gpt-4o-mini"
        else:
            return "gpt-4o"

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using Codex/OpenAI.
        """
        if not self._client:
            return "Session summary unavailable (Codex client not initialized)"

        # Build formatted context for prompt template
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

        try:
            response = await self._client.chat.completions.create(
                model=self._get_model("summary"),
                messages=[
                    {
                        "role": "system",
                        "content": "You are a session summary generator. Create comprehensive, actionable summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=4000,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self.logger.error(f"Failed to generate summary with Codex: {e}")
            return f"Session summary generation failed: {e}"

    async def synthesize_title(
        self, user_prompt: str, prompt_template: str | None = None
    ) -> str | None:
        """
        Synthesize session title using Codex/OpenAI.
        """
        if not self._client:
            return None

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for synthesize_title. "
                "Configure 'title_synthesis.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(user_prompt=user_prompt)

        try:
            response = await self._client.chat.completions.create(
                model=self._get_model("title"),
                messages=[
                    {
                        "role": "system",
                        "content": "You are a session title generator. Create concise, descriptive titles.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=50,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            self.logger.error(f"Failed to synthesize title with Codex: {e}")
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
        Execute code using Codex CLI's sandbox.

        Uses `codex exec` with sandbox mode for safe code execution.
        Falls back to error if Codex CLI is not available.
        """
        if not self._codex_cli_available:
            return {
                "success": False,
                "error": "Code execution requires Codex CLI. "
                "Install with: npm install -g @openai/codex",
                "language": language,
            }

        if language.lower() != "python":
            return {
                "success": False,
                "error": f"Language '{language}' not supported. Only Python is currently supported.",
                "language": language,
            }

        # Build task description for Codex
        if context:
            task = f"""Execute the following Python code and return the result.

Context: {context}

Code:
```python
{code}
```

Execute the code and return only the output."""
        else:
            task = f"""Execute the following Python code and return the result.

Code:
```python
{code}
```

Execute the code and return only the output."""

        # Use codex exec with JSON output
        actual_timeout = timeout if timeout is not None else 30

        try:
            # Run codex exec asynchronously
            process = await asyncio.create_subprocess_exec(
                "codex",
                "exec",
                "--json",
                task,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=actual_timeout)

            if process.returncode == 0:
                try:
                    result = json.loads(stdout.decode())
                    return {
                        "success": True,
                        "result": result.get("output", stdout.decode()),
                        "language": language,
                        "context": context,
                    }
                except json.JSONDecodeError:
                    return {
                        "success": True,
                        "result": stdout.decode().strip(),
                        "language": language,
                        "context": context,
                    }
            else:
                return {
                    "success": False,
                    "error": stderr.decode() or "Codex exec failed",
                    "error_type": "CodexExecError",
                    "language": language,
                }

        except TimeoutError:
            return {
                "success": False,
                "error": f"Code execution timed out after {actual_timeout} seconds",
                "error_type": "TimeoutError",
                "timeout": actual_timeout,
            }
        except Exception as e:
            self.logger.error(f"Codex code execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "language": language,
            }
