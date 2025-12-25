"""
Abstract base class for LLM providers.
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

# Auth mode type for providers
AuthMode = Literal["subscription", "api_key", "adc"]


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Defines the interface for generating summaries, synthesizing titles,
    and executing code across different providers (Claude, Codex, Gemini, LiteLLM).

    Properties:
        provider_name: Unique identifier for this provider (e.g., "claude", "codex")
        auth_mode: How this provider authenticates ("subscription", "api_key", "adc")
        supports_code_execution: Whether this provider can execute code in a sandbox
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the unique provider name.

        Returns:
            Provider name string (e.g., "claude", "codex", "gemini", "litellm")
        """
        pass

    @property
    def auth_mode(self) -> AuthMode:
        """
        Return the authentication mode for this provider.

        Default implementation returns "subscription". Override in subclasses
        that use different auth modes.

        Returns:
            Authentication mode: "subscription", "api_key", or "adc"
        """
        return "subscription"

    @property
    def supports_code_execution(self) -> bool:
        """
        Return whether this provider supports sandbox code execution.

        Default implementation returns False. Override in subclasses
        that have sandbox code execution capabilities.

        Returns:
            True if provider can execute code in a sandbox
        """
        return False

    @abstractmethod
    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        """
        Generate session summary.

        Args:
            context: Dictionary containing transcript turns, git status, etc.
            prompt_template: Optional override for the prompt.

        Returns:
            Generated summary string.
        """
        pass

    @abstractmethod
    async def synthesize_title(
        self, user_prompt: str, prompt_template: str | None = None
    ) -> str | None:
        """
        Synthesize session title.

        Args:
            user_prompt: The first user message.
            prompt_template: Optional override for the prompt.

        Returns:
            Synthesized title or None if failed.
        """
        pass

    @abstractmethod
    async def execute_code(
        self,
        code: str,
        language: str = "python",
        context: str | None = None,
        timeout: int | None = None,
        prompt_template: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute code in a sandbox.

        Args:
            code: Code to execute.
            language: Language (default: python).
            context: Context/instructions for the execution.
            timeout: Timeout in seconds.
            prompt_template: Optional override for the prompt.

        Returns:
            Dict with 'success', 'result', 'error', etc.
        """
        pass
