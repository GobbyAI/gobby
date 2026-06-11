"""
Feature-routed LLM service entry points.

Usage:
service = create_llm_service(config)
summary = await service.call_feature(config.session_summary, "Summarize this session.")
"""

from typing import TYPE_CHECKING

from gobby.llm.base import AuthMode
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    ToolResultEvent,
)

if TYPE_CHECKING:
    from gobby.llm.factory import create_llm_service
    from gobby.llm.local import LocalLLMProvider
    from gobby.llm.service import LLMService

__all__ = [
    "AuthMode",
    "ChatEvent",
    "DoneEvent",
    "LLMService",
    "LocalLLMProvider",
    "ToolResultEvent",
    "create_llm_service",
]


def __getattr__(name: str) -> object:
    """Load heavyweight exports lazily to avoid package import cycles."""
    if name == "create_llm_service":
        from gobby.llm.factory import create_llm_service

        return create_llm_service
    if name == "LocalLLMProvider":
        from gobby.llm.local import LocalLLMProvider

        return LocalLLMProvider
    if name == "LLMService":
        from gobby.llm.service import LLMService

        return LLMService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
