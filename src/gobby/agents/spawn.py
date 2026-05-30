"""Terminal spawning for agent execution.

This module provides PreparedSpawn helpers for spawning CLI agents.
The actual terminal spawning is handled by :class:`TmuxSpawner`
(re-exported here for backward compatibility).

Implementation is split across submodules:
- spawners/prompt_manager.py: Prompt file creation and cleanup
- spawners/command_builder.py: CLI command construction
- agents/tmux/spawner.py: TmuxSpawner (sole terminal backend)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gobby.agents.constants import get_terminal_env_vars
from gobby.agents.session import ChildSessionConfig, ChildSessionManager
from gobby.agents.spawners import (
    MAX_ENV_PROMPT_LENGTH,
    SpawnResult,
    TerminalSpawnerBase,
    build_cli_command,
    create_prompt_file,
)
from gobby.agents.tmux.spawner import TmuxSpawner

# Re-export TmuxSpawner under the old name for callers that still
# reference ``TerminalSpawner`` in patch targets or imports.
TerminalSpawner = TmuxSpawner

__all__ = [
    # Result dataclasses
    "SpawnResult",
    # Base class
    "TerminalSpawnerBase",
    # Spawner (tmux-only)
    "TmuxSpawner",
    "TerminalSpawner",  # backward compat alias
    # Helpers
    "PreparedSpawn",
    "prepare_terminal_spawn",
    "build_cli_command",
    "create_prompt_file",
    "MAX_ENV_PROMPT_LENGTH",
]

logger = logging.getLogger(__name__)


@dataclass
class PreparedSpawn:
    """Configuration for a prepared terminal spawn."""

    session_id: str
    """The pre-created child session ID."""

    agent_run_id: str
    """The agent run record ID."""

    parent_session_id: str
    """The parent session ID."""

    project_id: str
    """The project ID."""

    workflow_name: str | None
    """Workflow to activate (if any)."""

    agent_depth: int
    """Current agent depth."""

    env_vars: dict[str, str]
    """Environment variables to set."""

    seq_num: int | None = None
    """Session sequence number for human-friendly references."""


def prepare_terminal_spawn(
    session_manager: ChildSessionManager,
    parent_session_id: str,
    project_id: str,
    machine_id: str,
    source: str = "claude",
    agent_id: str | None = None,
    workflow_name: str | None = None,
    agent_name: str | None = None,
    initial_variables: dict[str, Any] | None = None,
    title: str | None = None,
    git_branch: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    is_local: bool = False,
    max_agent_depth: int = 5,
    agent_run_id: str | None = None,
    task_id: str | None = None,
    claimed_session_id: str | None = None,
    timeout_seconds: float | None = None,
    sandbox_enabled: bool = False,
    requested_reasoning_effort: str | None = None,
    effective_reasoning_effort: str | None = None,
    reasoning_required: bool = False,
    reasoning_status: str = "not_requested",
    reasoning_message: str | None = None,
    resume_metadata_json: dict[str, Any] | None = None,
) -> PreparedSpawn:
    """
    Prepare a terminal spawn by creating the child session.

    This should be called before spawning a terminal to:
    1. Create the child session in the database
    2. Generate the agent run ID
    3. Build the environment variables

    Args:
        session_manager: ChildSessionManager for session creation
        parent_session_id: Parent session ID
        project_id: Project ID
        machine_id: Machine ID
        source: CLI source (claude, gemini, qwen, codex, droid)
        agent_id: Optional agent ID
        workflow_name: Optional workflow to activate
        agent_name: Agent definition name used for the spawned session/run
        title: Optional session title
        git_branch: Optional git branch
        prompt: Optional initial prompt
        model: Optional model override.
        is_local: Whether the spawned runtime uses a local model endpoint.
        max_agent_depth: Maximum agent depth
        task_id: Optional task ID to link to the agent
        claimed_session_id: Session that owned the task when the run was created.
        timeout_seconds: Optional timeout for the agent run in seconds.
        sandbox_enabled: Whether the spawned runtime should be recorded as sandboxed.
        resume_metadata_json: Optional daemon-stop resume metadata snapshot to persist
            on the created agent run.

    Returns:
        PreparedSpawn with all necessary spawn configuration

    Raises:
        ValueError: If max agent depth exceeded
    """
    import uuid

    # Create child session config
    config = ChildSessionConfig(
        parent_session_id=parent_session_id,
        project_id=project_id,
        machine_id=machine_id,
        source=source,
        agent_id=agent_id,
        workflow_name=workflow_name,
        title=title,
        git_branch=git_branch,
        is_local=is_local,
        sandbox_enabled=sandbox_enabled,
    )

    # Create the child session
    child_session = session_manager.create_child_session(config)

    # Write initial variables to session_variables table (canonical store)
    if initial_variables:
        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(session_manager._storage.db).merge_variables(
            child_session.id, initial_variables
        )

    # Use provided agent_run_id or generate one (backward compat)
    if agent_run_id is None:
        agent_run_id = f"run-{uuid.uuid4().hex[:12]}"

    # Create agent_runs record so the FK constraint on sessions.agent_run_id is satisfied.
    import logging as _logging

    from gobby.storage.agents import LocalAgentRunManager

    _pts_logger = _logging.getLogger("agents.spawn.prepare_terminal_spawn")
    _pts_logger.info(f"Creating agent_run {agent_run_id} for child_session {child_session.id}")

    agent_run_mgr = LocalAgentRunManager(session_manager._storage.db)
    agent_run_mgr.create(
        parent_session_id=parent_session_id,
        provider=source,
        prompt=prompt or "",
        workflow_name=workflow_name,
        agent_name=agent_name,
        model=model,
        is_local=is_local,
        child_session_id=child_session.id,
        claimed_session_id=claimed_session_id,
        run_id=agent_run_id,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        requested_reasoning_effort=requested_reasoning_effort,
        effective_reasoning_effort=effective_reasoning_effort,
        reasoning_required=reasoning_required,
        reasoning_status=reasoning_status,
        reasoning_message=reasoning_message,
        resume_metadata_json=resume_metadata_json,
    )

    # Persist agent_run_id to session record for hook-based lifecycle tracking
    session_manager.update_terminal_pickup_metadata(
        session_id=child_session.id,
        agent_run_id=agent_run_id,
        workflow_name=workflow_name,
    )

    # Handle prompt - decide env var vs file
    prompt_env: str | None = None
    prompt_file: str | None = None

    if prompt:
        if len(prompt) <= MAX_ENV_PROMPT_LENGTH:
            prompt_env = prompt
        else:
            # Write to temp file with secure permissions
            prompt_file = create_prompt_file(prompt, child_session.id)

    # Build environment variables
    env_vars = get_terminal_env_vars(
        session_id=child_session.id,
        parent_session_id=parent_session_id,
        agent_run_id=agent_run_id,
        project_id=project_id,
        workflow_name=workflow_name,
        agent_depth=child_session.agent_depth,
        max_agent_depth=max_agent_depth,
        prompt=prompt_env,
        prompt_file=prompt_file,
    )

    return PreparedSpawn(
        session_id=child_session.id,
        agent_run_id=agent_run_id,
        parent_session_id=parent_session_id,
        project_id=project_id,
        workflow_name=workflow_name,
        agent_depth=child_session.agent_depth,
        env_vars=env_vars,
        seq_num=getattr(child_session, "seq_num", None),
    )
