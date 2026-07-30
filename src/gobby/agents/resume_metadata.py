"""Agent launch snapshots used for daemon-stop resume."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_cache_policy import SPAWN_CACHE_ENV_VARS

RESUME_METADATA_VERSION = 1
_RESUME_METADATA_ENV_KEYS = frozenset(SPAWN_CACHE_ENV_VARS)

# Positive allowlist of config-override keys persisted for daemon-stop resume.
# Secret-bearing overrides are never persisted: resume re-mints the agent
# capability from the operator token instead of replaying a stored one.
_RESUME_CONFIG_OVERRIDE_KEYS = frozenset(
    {
        "mcp_servers.gobby.command",
        "mcp_servers.gobby.args",
        "mcp_servers.gobby.startup_timeout_sec",
        "mcp_servers.gobby.tool_timeout_sec",
        "mcp_servers.gobby.env_vars",
        "mcp_servers.gobby.env.TMPDIR",
        "mcp_servers.gobby.env.GOBBY_SESSION_ID",
        "mcp_servers.gobby.env.GOBBY_PROJECT_ID",
        "mcp_servers.gobby.env.GOBBY_AGENT_RUN_ID",
        "sandbox_mode",
        "model",
        "model_provider",
        "shell_environment_policy.exclude",
        "features.shell_snapshot",
    }
)
_RESUME_CONFIG_OVERRIDE_KEY_PREFIXES = (
    # Per-tool approval table re-seeded for the Gobby MCP proxy.
    "mcp_servers.gobby.tools.",
    # Named generation-endpoint provider config; secret-free by construction
    # (codex_endpoint_config_overrides stores the API-key env *name* only).
    "model_providers.",
)


def filter_resume_config_overrides(overrides: Any) -> list[str]:
    """Keep only allowlisted non-secret config overrides for persistence."""
    kept: list[str] = []
    if not isinstance(overrides, list | tuple):
        return kept
    for override in overrides:
        if not isinstance(override, str):
            continue
        key = override.partition("=")[0].strip()
        if key in _RESUME_CONFIG_OVERRIDE_KEYS or key.startswith(
            _RESUME_CONFIG_OVERRIDE_KEY_PREFIXES
        ):
            kept.append(override)
    return kept


def json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy, stringifying unknown leaf values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        items = [json_safe(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def normalize_resume_metadata(value: Any) -> dict[str, Any] | None:
    """Normalize a resume metadata DB value into a dict."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(value, Mapping):
        return dict(value)
    return None


def dump_resume_metadata(value: Mapping[str, Any] | None) -> str | None:
    """Serialize resume metadata for JSONB storage."""
    if value is None:
        return None
    return json.dumps(json_safe(dict(value)), sort_keys=True, separators=(",", ":"))


def merge_resume_metadata_env(*sources: Any) -> dict[str, str]:
    """Return non-secret env values needed to reproduce spawned-agent caches."""
    final_env: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            if key in _RESUME_METADATA_ENV_KEYS:
                final_env[str(key)] = str(value)
    return final_env


def build_resume_metadata(
    *,
    provider: str,
    model: str | None,
    requested_reasoning_effort: str | None,
    effective_reasoning_effort: str | None,
    reasoning_required: bool,
    reasoning_status: str,
    reasoning_message: str | None,
    sandbox_config: SandboxConfig,
    cwd: str,
    project_id: str,
    project_path: str,
    parent_session_id: str,
    isolation: str,
    worktree_id: str | None,
    clone_id: str | None,
    branch_name: str | None,
    base_branch: str,
    base_commit_sha: str | None,
    task_id: str | None,
    task_ref: str | None,
    stage_name: str | None,
    stage_state: str | None,
    agent_slug: str | None,
    workflow: str | None,
    initial_variables: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the normalized launch snapshot persisted on agent_runs."""
    return cast(
        dict[str, Any],
        json_safe(
            {
                "version": RESUME_METADATA_VERSION,
                "provider": provider,
                "model": model,
                "requested_reasoning_effort": requested_reasoning_effort,
                "effective_reasoning_effort": effective_reasoning_effort,
                "reasoning_required": reasoning_required,
                "reasoning_status": reasoning_status,
                "reasoning_message": reasoning_message,
                "sandbox_config": sandbox_config.model_dump(),
                # Resumed autonomous runs must be able to continue without a
                # provider approval prompt after daemon-triggered recovery.
                "approval_mode": "auto",
                "auto_approve": True,
                "cwd": cwd,
                "workspace_path": cwd,
                "project_id": project_id,
                "project_path": project_path,
                "parent_session_id": parent_session_id,
                "isolation": isolation,
                "worktree_id": worktree_id,
                "clone_id": clone_id,
                "branch_name": branch_name,
                "base_branch": base_branch,
                "base_commit_sha": base_commit_sha,
                "task_id": task_id,
                "task_ref": task_ref,
                "stage_name": stage_name,
                "stage_state": stage_state,
                "agent_slug": agent_slug,
                "workflow": workflow,
                "initial_variables": dict(initial_variables),
                "provider_native_session_id": None,
                "env": {},
                "sandbox_args": [],
                "sandbox_env": {},
                "config_overrides": [],
            },
        ),
    )
