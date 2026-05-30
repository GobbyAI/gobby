"""Dataclasses shared by spawned-agent launch code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gobby.agents.sandbox import SandboxConfig


@dataclass
class SpawnRequest:
    """Request for spawning an agent."""

    prompt: str
    cwd: str
    provider: str
    session_id: str
    run_id: str
    parent_session_id: str
    project_id: str
    project_path: str | None = None
    agent_run_id: str | None = None
    workflow: str | None = None
    initial_variables: dict[str, Any] | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    branch_name: str | None = None
    task_id: str | None = None
    claimed_session_id: str | None = None
    title: str | None = None
    agent_name: str | None = None
    agent_depth: int = 0
    max_agent_depth: int = 5
    session_manager: Any | None = None
    machine_id: str | None = None
    model: str | None = None
    is_local: bool = False
    api_base: str | None = None
    api_token: str | None = None
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    reasoning_required: bool = False
    reasoning_status: str = "not_requested"
    reasoning_message: str | None = None
    sandbox_config: SandboxConfig | None = None
    sandbox_args: list[str] | None = None
    sandbox_env: dict[str, str] | None = field(default=None)
    extra_env: dict[str, str] | None = field(default=None)
    timeout_seconds: float | None = None
    daemon_config: Any | None = None
    resume_metadata_json: dict[str, Any] | None = None


@dataclass
class SpawnResult:
    """Result of a spawn operation."""

    success: bool
    run_id: str
    child_session_id: str | None
    status: str
    pid: int | None = None
    terminal_type: str | None = None
    error: str | None = None
    message: str | None = None
    codex_session_id: str | None = None
    tmux_session_name: str | None = None
    tmux_socket_name: str | None = None
    tmux_socket_path: str | None = None
