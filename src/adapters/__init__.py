"""CLI adapters for multi-CLI session management.

This module contains adapters that translate between CLI-specific hook formats
and the unified HookEvent/HookResponse models.

Each adapter is responsible for:
1. Translating native CLI payloads to HookEvent
2. Translating HookResponse back to CLI-expected format
3. Managing CLI-specific session lifecycle

Adapters:
- ClaudeCodeAdapter: For Claude Code CLI hooks (HTTP-based)
- GeminiAdapter: For Gemini CLI hooks (HTTP-based) [Phase 3]
- CodexAdapter: For Codex CLI via app-server (JSON-RPC-based) [Phase 4]
- CodexNotifyAdapter: For Codex CLI notify events (simple HTTP-based)
"""

from gobby.adapters.base import BaseAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex import CodexAdapter, CodexAppServerClient, CodexNotifyAdapter
from gobby.adapters.gemini import GeminiAdapter

__all__ = [
    "BaseAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CodexAppServerClient",
    "CodexNotifyAdapter",
    "GeminiAdapter",
]
