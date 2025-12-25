"""
LiteLLM implementation of LLMProvider.

LiteLLM provides a unified interface to many LLM providers (OpenAI, Anthropic,
Mistral, Cohere, etc.) through their APIs using BYOK (Bring Your Own Key).

This provider is useful when users want to use their own API keys for
multiple different providers without needing separate provider implementations.
"""

import json
import logging
from typing import Any

from gobby.config.app import DaemonConfig
from gobby.llm.base import AuthMode, LLMProvider

logger = logging.getLogger(__name__)


class LiteLLMProvider(LLMProvider):  # type: ignore[misc]
    """
    LiteLLM implementation of LLMProvider.

    Uses API key-based authentication (BYOK) for multiple providers.
    API keys are read from:
    1. llm_providers.api_keys in config (e.g., OPENAI_API_KEY, MISTRAL_API_KEY)
    2. Environment variables as fallback

    Example config:
        llm_providers:
          litellm:
            models: gpt-4o-mini,mistral-large
            auth_mode: api_key
          api_keys:
            OPENAI_API_KEY: sk-...
            MISTRAL_API_KEY: ...
    """

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "litellm"

    @property
    def auth_mode(self) -> AuthMode:
        """LiteLLM uses API key authentication."""
        return "api_key"

    def __init__(self, config: DaemonConfig):
        """
        Initialize LiteLLMProvider.

        Args:
            config: Client configuration with optional api_keys in llm_providers.
        """
        self.config = config
        self.logger = logger
        self._litellm = None
        self._api_keys: dict[str, str] = {}

        # Load API keys from config
        if config.llm_providers and config.llm_providers.api_keys:
            self._api_keys = config.llm_providers.api_keys.copy()

        try:
            import litellm

            self._litellm = litellm

            # Set API keys in litellm's environment
            # LiteLLM reads from os.environ, so we set them there
            import os

            for key, value in self._api_keys.items():
                if value and key not in os.environ:
                    os.environ[key] = value
                    self.logger.debug(f"Set {key} from config")

            self.logger.debug("LiteLLM provider initialized")

        except ImportError:
            self.logger.error(
                "litellm package not found. Please install with `pip install litellm`."
            )
        except Exception as e:
            self.logger.error(f"Failed to initialize LiteLLM: {e}")

    def _get_model(self, task: str) -> str:
        """
        Get the model to use for a specific task.

        Args:
            task: Task type ("summary" or "title")

        Returns:
            Model name string
        """
        if task == "summary":
            return self.config.session_summary.model or "gpt-4o-mini"
        elif task == "title":
            return self.config.title_synthesis.model or "gpt-4o-mini"
        else:
            return "gpt-4o-mini"

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using LiteLLM.
        """
        if not self._litellm:
            return "Session summary unavailable (LiteLLM not initialized)"

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
            # Use LiteLLM's async completion
            response = await self._litellm.acompletion(
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
            self.logger.error(f"Failed to generate summary with LiteLLM: {e}")
            return f"Session summary generation failed: {e}"

    async def synthesize_title(
        self, user_prompt: str, prompt_template: str | None = None
    ) -> str | None:
        """
        Synthesize session title using LiteLLM.
        """
        if not self._litellm:
            return None

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for synthesize_title. "
                "Configure 'title_synthesis.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(user_prompt=user_prompt)

        try:
            response = await self._litellm.acompletion(
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
            self.logger.error(f"Failed to synthesize title with LiteLLM: {e}")
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
        Execute code using LiteLLM.

        NOTE: Not supported as LiteLLM providers don't have sandbox access.
        """
        return {
            "success": False,
            "error": "Code execution is not supported for LiteLLM provider. "
            "LiteLLM providers do not have sandbox access. "
            "Use Claude provider for code execution.",
            "language": language,
        }
