"""
LLM Provider Abstraction for Gobby Client.

This module provides interfaces and implementations for different LLM providers
(Claude, Codex, Gemini, LiteLLM) to make the client CLI-agnostic.

Usage:
    service = create_llm_service(config)
    provider, model, prompt = service.get_provider_for_feature(config.session_summary)
"""

from gobby.llm.base import AuthMode, LLMProvider
from gobby.llm.claude_executor import ClaudeExecutor
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    MCPToolResult,
    TextChunk,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.llm.executor import (
    AgentExecutor,
    AgentResult,
    ToolCallRecord,
    ToolHandler,
    ToolResult,
    ToolSchema,
)
from gobby.llm.factory import create_llm_service
from gobby.llm.gemini_executor import GeminiExecutor
from gobby.llm.openai_executor import OpenAIExecutor
from gobby.llm.service import LLMService

__all__ = [
    "AgentExecutor",
    "AgentResult",
    "AuthMode",
    "ChatEvent",
    "ClaudeExecutor",
    "DoneEvent",
    "GeminiExecutor",
    "LLMProvider",
    "LLMService",
    "MCPToolResult",
    "TextChunk",
    "ToolCall",
    "ToolCallEvent",
    "ToolCallRecord",
    "ToolHandler",
    "ToolResult",
    "ToolResultEvent",
    "OpenAIExecutor",
    "ToolSchema",
    "create_llm_service",
]
