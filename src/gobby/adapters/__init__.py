"""CLI adapters for multi-CLI session management.

This module contains adapters that translate between CLI-specific hook formats
and the unified HookEvent/HookResponse models.

Each adapter is responsible for:
1. Translating native CLI payloads to HookEvent
2. Translating HookResponse back to CLI-expected format
3. Managing CLI-specific session lifecycle

Adapters:
- ClaudeCodeAdapter: For Claude Code CLI hooks (HTTP-based)
- AgyAdapter: For AGY CLI hooks (HTTP-based)
- DroidAdapter: For Factory Droid CLI hooks (HTTP-based)
- CodexAdapter: For Codex CLI via app-server (JSON-RPC-based) [Phase 4]
- CodexHooksAdapter: For Codex CLI hooks.json lifecycle events
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.adapters.agy import AgyAdapter
    from gobby.adapters.agy_contract import AGY_HOOK_NAMES
    from gobby.adapters.base import BaseAdapter
    from gobby.adapters.capabilities import get_provider_capabilities
    from gobby.adapters.claude_code import ClaudeCodeAdapter
    from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
    from gobby.adapters.codex_impl.client import CodexAppServerClient
    from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
    from gobby.adapters.droid import DroidAdapter
    from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
    from gobby.adapters.git import GitAdapter
    from gobby.adapters.grok import GrokAdapter

# Each public name loads its module on first access, so importing one adapter module does
# not load every adapter and the agent stack behind them.
_EXPORTS: dict[str, str] = {
    "AGY_HOOK_NAMES": "gobby.adapters.agy_contract",
    "AgyAdapter": "gobby.adapters.agy",
    "BaseAdapter": "gobby.adapters.base",
    "ClaudeCodeAdapter": "gobby.adapters.claude_code",
    "CodexAdapter": "gobby.adapters.codex_impl.app_server_adapter",
    "CodexAppServerClient": "gobby.adapters.codex_impl.client",
    "CodexHooksAdapter": "gobby.adapters.codex_impl.hooks_adapter",
    "DROID_PASCAL_HOOK_NAMES": "gobby.adapters.droid_contract",
    "DroidAdapter": "gobby.adapters.droid",
    "GitAdapter": "gobby.adapters.git",
    "GrokAdapter": "gobby.adapters.grok",
    "get_provider_capabilities": "gobby.adapters.capabilities",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "BaseAdapter",
    "AGY_HOOK_NAMES",
    "AgyAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CodexAppServerClient",
    "CodexHooksAdapter",
    "DROID_PASCAL_HOOK_NAMES",
    "DroidAdapter",
    "GitAdapter",
    "GrokAdapter",
    "get_provider_capabilities",
]
