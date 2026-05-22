"""CLI adapters for multi-CLI session management.

This module contains adapters that translate between CLI-specific hook formats
and the unified HookEvent/HookResponse models.

Each adapter is responsible for:
1. Translating native CLI payloads to HookEvent
2. Translating HookResponse back to CLI-expected format
3. Managing CLI-specific session lifecycle

Adapters:
- ClaudeCodeAdapter: For Claude Code CLI hooks (HTTP-based)
- DroidAdapter: For Factory Droid CLI hooks (HTTP-based)
- GeminiAdapter: For Gemini CLI hooks (HTTP-based) [Phase 3]
- CodexAdapter: For Codex CLI via app-server (JSON-RPC-based) [Phase 4]
- CodexHooksAdapter: For Codex CLI hooks.json lifecycle events
"""

from gobby.adapters.base import BaseAdapter
from gobby.adapters.capabilities import get_provider_capabilities
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter, CodexNotifyAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.adapters.gemini import GeminiAdapter

__all__ = [
    "BaseAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CodexAppServerClient",
    "CodexHooksAdapter",
    "CodexNotifyAdapter",
    "DROID_PASCAL_HOOK_NAMES",
    "DroidAdapter",
    "GeminiAdapter",
    "get_provider_capabilities",
]
