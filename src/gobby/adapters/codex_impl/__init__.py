"""
Codex adapter implementation package.

This package contains the decomposed implementation of the Codex adapter,
extracted from the monolithic codex.py using the Strangler Fig pattern.

Modules:
- types.py: Type definitions and data classes
- protocol.py: Protocol/interface definitions
- client.py: CodexAppServerClient implementation
- app_server_adapter.py: CodexAdapter implementation
- hooks_adapter.py: CodexHooksAdapter and CodexNotifyAdapter implementations

Importer analysis (from codex.py):
- src/gobby/servers/app_factory.py: imports CodexAdapter from app_server_adapter
- src/gobby/servers/routes/mcp/hooks.py: imports CodexHooksAdapter from hooks_adapter
- src/gobby/adapters/__init__.py: imports public Codex adapter APIs
- tests/adapters/test_codex.py: imports canonical implementation modules

Migration strategy:
1. Extract types/dataclasses to types.py
2. Extract protocol definitions to protocol.py
3. Extract CodexAppServerClient to client.py
4. Extract adapters to app_server_adapter.py and hooks_adapter.py
5. Update codex.py to re-export from submodules
"""

# Phase 3: Placeholders - exports will be added as code is migrated
__all__: list[str] = []
