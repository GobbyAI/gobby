"""Dataclasses shared by spawned-agent launch code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import KW_ONLY, dataclass, field
from datetime import datetime
from subprocess import SubprocessError
from typing import TYPE_CHECKING, Any, Literal

from gobby.agents.sandbox import SandboxConfig
from gobby.config.terminals import TerminalConfig
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.runtime import TerminalSpawnFailed

if TYPE_CHECKING:
    from gobby.agents.session import ChildSessionManager
    from gobby.agents.spawn import PreparedSpawn
    from gobby.config.app import DaemonConfig
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.terminals import AttachLocator, TerminalManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.write_coordinator import WriteCoordinator


def resolve_terminal_backend(
    requested: str | None,
    daemon_config: Any | None,
) -> Literal["tmux", "native"]:
    """Validate an explicit backend or fall back to TerminalConfig.default_backend."""
    if requested is None:
        config = getattr(daemon_config, "terminals", None)
        if isinstance(config, TerminalConfig):
            return config.default_backend
        return TerminalConfig().default_backend
    if requested == "native":
        return "native"
    if requested == "tmux":
        return "tmux"
    raise ValueError(f"invalid terminal_backend: {requested}")


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
    _: KW_ONLY
    project_path: str | None = None
    agent_run_id: str | None = None
    workflow: str | None = None
    initial_variables: dict[str, Any] | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    branch_name: str | None = None
    task_id: str | None = None
    claimed_session_id: str | None = None
    agent_name: str | None = None
    agent_depth: int = 0
    max_agent_depth: int = 5
    session_manager: ChildSessionManager | None = None
    run_manager: LocalAgentRunManager | None = None
    cleanup_agent: Callable[..., Awaitable[None]] | None = None
    machine_id: str | None = None
    model: str | None = None
    is_local: bool = False
    codex_oss_provider: str | None = None
    codex_config_overrides: tuple[str, ...] = ()
    api_base: str | None = None
    api_token: str | None = None
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    reasoning_required: bool = False
    reasoning_status: str = "not_requested"
    reasoning_message: str | None = None
    auto_approve: bool = True
    provider_args: tuple[str, ...] = ()
    sandbox_config: SandboxConfig | None = None
    sandbox_args: list[str] | None = None
    sandbox_env: dict[str, str] | None = None
    extra_env: dict[str, str] | None = None
    timeout_seconds: float | None = None
    daemon_config: DaemonConfig | None = None
    resume_metadata_json: dict[str, Any] | None = None
    code_index_preflight_mode: str | None = None
    code_index_api_token: str | None = None
    code_index_preflight_warning: dict[str, str] | None = None
    prepared_spawn: PreparedSpawn
    phase_timings_ms: dict[str, float] = field(default_factory=dict)
    terminal_manager: TerminalManager | None = None
    terminal_runtime_registry: TerminalRuntimeRegistry | None = None
    write_coordinator: WriteCoordinator | None = None
    backend: Literal["tmux", "native"] | None = None
    terminal_backend: Literal["tmux", "native"] = "tmux"
    droid_mode: Literal["exec", "interactive"] = "exec"
    retry_terminal_id: str | None = None
    cancel_event: asyncio.Event | None = None
    # Binds the reserved pane to the terminal id after ``create_pending`` and
    # before exec; set only for a placed spawn.
    placement_binder: Callable[[str], Awaitable[None]] | None = None


@dataclass
class SpawnResult:
    """Result of a spawn operation."""

    success: bool
    run_id: str
    child_session_id: str | None
    status: str
    pid: int | None = None
    backend: str | None = None
    error: str | None = None
    error_detail: str | None = None
    message: str | None = None
    codex_session_id: str | None = None
    terminal_id: str | None = None
    locator: AttachLocator | None = None
    retryable_infrastructure: bool = False
    # A failed retry's pre-bump attempt pair (None for a create); failure cleanup
    # uses it to recognize a rolled-back bump.
    prior_attempt: tuple[int, datetime] | None = None


def is_infrastructure_spawn_error(error: BaseException) -> bool:
    """Keep malformed/configuration failures distinct from transport/process failures."""
    known = (OSError, SubprocessError, HostUnavailableError)
    return isinstance(error, known) or (
        isinstance(error, TerminalSpawnFailed) and isinstance(error.__cause__, known)
    )
