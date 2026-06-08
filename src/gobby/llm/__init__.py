"""
Feature-routed LLM service entry points.

Usage:
service = create_llm_service(config)
summary = await service.call_feature(config.session_summary, "Summarize this session.")
"""

from gobby.llm.base import AuthMode
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
    "LLMService",
    "LocalLLMProvider",
    "ToolResultEvent",
    "create_llm_service",
]
