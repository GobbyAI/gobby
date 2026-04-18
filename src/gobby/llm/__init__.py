"""
LLM Provider Abstraction for Gobby Client.

This module provides interfaces and implementations for Gobby's LLM providers
to make the client CLI-agnostic.

Usage:
    service = create_llm_service(config)
    provider, model, prompt = service.get_provider_for_feature(config.session_summary)
"""

from gobby.llm.base import AuthMode, LLMProvider
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    ToolResultEvent,
)
from gobby.llm.factory import create_llm_service
from gobby.llm.local import LocalLLMProvider
from gobby.llm.service import LLMService

__all__ = [
    "AuthMode",
    "ChatEvent",
    "DoneEvent",
    "LLMProvider",
    "LLMService",
    "LocalLLMProvider",
    "ToolResultEvent",
    "create_llm_service",
]
