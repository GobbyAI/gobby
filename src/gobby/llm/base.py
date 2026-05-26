"""
Abstract base class for LLM providers.
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

# Auth mode type for providers
AuthMode = Literal["subscription", "api_key", "adc"]


class LLMProviderCancellation(RuntimeError):
    """Raised when an LLM provider operation is cancelled by shutdown/termination."""


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Defines the interface for generating summaries and synthesizing titles
    across different providers (Claude, Gemini, Codex).

    Properties:
        provider_name: Unique identifier for this provider (e.g., "claude", "gemini", "codex")
        auth_mode: How this provider authenticates ("subscription", "api_key", "adc")
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the unique provider name.

        Returns:
            Provider name string (e.g., "claude", "gemini", "codex")
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
    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> str:
        """
        Generate text from a prompt.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            model: Optional model override
            max_tokens: Optional max output tokens override
            caller: Optional feature slug for provider-side logging

        Returns:
            Generated text response
        """
        pass

    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a JSON response from a prompt.

        Like generate_text() but requests structured JSON output and
        returns a parsed dictionary. Uses response_format={'type':'json_object'}
        where supported by the provider.

        Args:
            prompt: User prompt (should instruct the model to return JSON)
            system_prompt: Optional system prompt
            model: Optional model override
            caller: Optional feature slug for provider-side logging

        Returns:
            Parsed JSON response as a dictionary

        Raises:
            ValueError: If the response is not valid JSON
            RuntimeError: If the LLM backend is not available
        """
        pass

    @abstractmethod
    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Generate a text description of an image.

        Used for multimodal memory support - converts images to text
        descriptions that can be stored alongside memory content.

        Args:
            image_path: Path to the image file to describe
            context: Optional context to guide the description
                    (e.g., "This is a screenshot of the settings page")

        Returns:
            Text description of the image suitable for memory storage
        """
        pass
