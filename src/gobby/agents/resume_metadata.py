"""Agent launch snapshots used for daemon-stop resume."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from gobby.agents.sandbox import SandboxConfig

RESUME_METADATA_VERSION = 1


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
