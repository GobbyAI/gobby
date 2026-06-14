"""
Compatibility facade for agent isolation handlers.

Implementation lives in focused sibling modules:
- isolation_models.py: shared dataclasses, branch names, ABC
- isolation_none.py: no-op handler
- isolation_worktree.py: git worktree handler
- isolation_clone.py: git clone handler and base commit capture
- isolation_repair.py: hook, project metadata, MCP config, provider preflight helpers
- isolation_code_index.py: gcode preflight wrapper
- isolation_factory.py: handler factory
"""

from __future__ import annotations

from gobby.agents.isolation_clone import CloneIsolationHandler, _capture_base_commit_sha
from gobby.agents.isolation_code_index import ensure_isolation_code_index
from gobby.agents.isolation_factory import get_isolation_handler
from gobby.agents.isolation_models import (
    IsolationContext,
    IsolationHandler,
    SpawnConfig,
    generate_branch_name,
)
from gobby.agents.isolation_none import NoneIsolationHandler
from gobby.agents.isolation_repair import (
    _copy_cli_hooks,
    _copy_droid_hooks_for_isolation,
    _load_droid_isolation_hooks_template,
    _load_json_object,
    _merge_droid_isolation_hooks,
    _patch_mcp_config_for_isolation,
    _write_droid_isolation_hooks,
    provider_mcp_config_error,
    repair_isolation_environment,
)
from gobby.agents.isolation_worktree import WorktreeIsolationHandler

__all__ = [
    "CloneIsolationHandler",
    "IsolationContext",
    "IsolationHandler",
    "NoneIsolationHandler",
    "SpawnConfig",
    "WorktreeIsolationHandler",
    "_capture_base_commit_sha",
    "_copy_cli_hooks",
    "_copy_droid_hooks_for_isolation",
    "_load_droid_isolation_hooks_template",
    "_load_json_object",
    "_merge_droid_isolation_hooks",
    "_patch_mcp_config_for_isolation",
    "_write_droid_isolation_hooks",
    "ensure_isolation_code_index",
    "generate_branch_name",
    "get_isolation_handler",
    "provider_mcp_config_error",
    "repair_isolation_environment",
]
