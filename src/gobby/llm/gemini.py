"""
Gemini implementation of LLMProvider.

Supports two authentication modes:
- api_key: Use GEMINI_API_KEY environment variable (BYOK)
- adc: Use Google Application Default Credentials (subscription-based via gcloud auth)
"""

import json
import logging
import os
from typing import Any, Literal

from gobby.config.app import DaemonConfig
from gobby.llm.base import AuthMode, LLMProvider

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):  # type: ignore[misc]
    """
    Gemini implementation of LLMProvider using google-generativeai package.

    Supports two authentication modes:
    - api_key: Use GEMINI_API_KEY environment variable (BYOK)
    - adc: Use Google Application Default Credentials (run `gcloud auth application-default login`)
    """

    def __init__(
        self,
        config: DaemonConfig,
        auth_mode: Literal["api_key", "adc"] | None = None,
    ):
        """
        Initialize GeminiProvider.

        Args:
            config: Client configuration.
            auth_mode: Override auth mode. If None, reads from config.llm_providers.gemini.auth_mode
                      or falls back to "api_key".
        """
        self.config = config
        self.logger = logger
        self.model_summary = None
        self.model_title = None

        # Determine auth mode from config or parameter
        self._auth_mode: AuthMode = "api_key"  # Default
        if auth_mode:
            self._auth_mode = auth_mode
        elif config.llm_providers and config.llm_providers.gemini:
            self._auth_mode = config.llm_providers.gemini.auth_mode

        try:
            import google.generativeai as genai

            # Initialize based on auth mode
            if self._auth_mode == "adc":
                # Use Application Default Credentials
                # User must run: gcloud auth application-default login
                try:
                    import google.auth

                    credentials, project = google.auth.default()
                    genai.configure(credentials=credentials)
                    self.genai = genai
                    self.logger.debug("Gemini initialized with ADC credentials")
                except Exception as e:
                    self.logger.error(
                        f"Failed to initialize Gemini with ADC: {e}. "
                        "Run 'gcloud auth application-default login' to authenticate."
                    )
                    self.genai = None
            else:
                # Use API key from environment
                api_key = os.environ.get("GEMINI_API_KEY")
                if api_key:
                    genai.configure(api_key=api_key)
                    self.genai = genai
                    self.logger.debug("Gemini initialized with API key")
                else:
                    self.logger.warning("GEMINI_API_KEY not found in environment variables.")
                    self.genai = None

            # Initialize models if genai is configured
            if self.genai:
                summary_model_name = self.config.session_summary.model or "gemini-1.5-pro"
                title_model_name = self.config.title_synthesis.model or "gemini-1.5-flash"

                self.model_summary = genai.GenerativeModel(summary_model_name)
                self.model_title = genai.GenerativeModel(title_model_name)

        except ImportError:
            self.logger.error(
                "google-generativeai package not found. Please install with `pip install google-generativeai`."
            )
            self.genai = None
        except Exception as e:
            self.logger.error(f"Failed to initialize Gemini client: {e}")
            self.genai = None

    @property
    def provider_name(self) -> str:
        """Return provider name."""
        return "gemini"

    @property
    def auth_mode(self) -> AuthMode:
        """Return the authentication mode for this provider."""
        return self._auth_mode

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary using Gemini.
        """
        if not self.genai or not self.model_summary:
            return "Session summary unavailable (Gemini client not initialized)"

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
            # Gemini async generation
            response = await self.model_summary.generate_content_async(prompt)
            return response.text or ""
        except Exception as e:
            self.logger.error(f"Failed to generate summary with Gemini: {e}")
            return f"Session summary generation failed: {e}"

    async def synthesize_title(
        self, user_prompt: str, prompt_template: str | None = None
    ) -> str | None:
        """
        Synthesize session title using Gemini.
        """
        if not self.genai or not self.model_title:
            return None

        # Build prompt - prompt_template is required
        if not prompt_template:
            raise ValueError(
                "prompt_template is required for synthesize_title. "
                "Configure 'title_synthesis.prompt' in ~/.gobby/config.yaml"
            )
        prompt = prompt_template.format(user_prompt=user_prompt)

        try:
            response = await self.model_title.generate_content_async(prompt)
            return (response.text or "").strip()
        except Exception as e:
            self.logger.error(f"Failed to synthesize title with Gemini: {e}")
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
        Execute code using Gemini.

        NOTE: Currently not supported as it requires a sandbox.
        """
        return {
            "success": False,
            "error": "Code execution is not yet supported for Gemini provider. A sandbox environment is required.",
            "language": language,
        }
