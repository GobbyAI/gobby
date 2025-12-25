"""
LLM Provider Abstraction for Gobby Client.

This module provides interfaces and implementations for different LLM providers
(Claude, Codex, Gemini, LiteLLM) to make the client CLI-agnostic.

Usage:
    service = create_llm_service(config)
    provider, model, prompt = service.get_provider_for_feature(config.session_summary)
"""

from gobby.llm.base import AuthMode, LLMProvider
from gobby.llm.factory import create_llm_service
from gobby.llm.service import LLMService

__all__ = ["AuthMode", "LLMProvider", "LLMService", "create_llm_service"]
