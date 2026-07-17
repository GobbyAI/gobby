"""Public surface for the daemon ``tool_chat`` (agentic) capability.

Façade over the ``_tool_chat_*`` modules, mirroring :mod:`gobby.ai.text_generation`.
"""

from __future__ import annotations

from gobby.ai._tool_chat_adapters import (
    ClaudeToolChatAdapter,
    OpenAICompatibleToolChatAdapter,
)
from gobby.ai._tool_chat_builder import build_daemon_tool_chat_service
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
    InvocationRecord,
)
from gobby.ai._tool_chat_contracts import (
    ToolChatAdapter,
    ToolChatRequest,
    ToolChatResult,
    ToolLoopConfigurationError,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.ai._tool_chat_service import ToolChatService

__all__ = [
    "ClaudeToolChatAdapter",
    "BuiltinExecutionContext",
    "BuiltinToolResult",
    "BuiltinToolSpec",
    "InvocationRecord",
    "OpenAICompatibleToolChatAdapter",
    "ToolChatAdapter",
    "ToolChatRequest",
    "ToolChatResult",
    "ToolChatService",
    "ToolLoopConfigurationError",
    "ToolLoopLimits",
    "ToolPolicy",
    "build_daemon_tool_chat_service",
]
