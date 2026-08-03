"""Terminal spawning for agent execution.

This module provides PreparedSpawn helpers for spawning CLI agents.
The actual terminal spawning is handled by :class:`TmuxSpawner`.

Implementation is split across submodules:
- spawners/prompt_manager.py: Prompt file creation and cleanup
- spawners/command_builder.py: CLI command construction
- agents/tmux/spawner.py: TmuxSpawner (sole terminal backend)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
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
from gobby.utils.local_token import read_local_api_token

__all__ = [
    # Result dataclasses
    "SpawnResult",
    # Base class
    "TerminalSpawnerBase",
    # Spawner (tmux-only)
    "TmuxSpawner",
    # Helpers
    "PreparedSpawn",
    "prepare_terminal_resume",
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
    machine_id: str | None,
    source: str = "claude",
    agent_id: str | None = None,
    workflow_name: str | None = None,
    agent_name: str | None = None,
    initial_variables: dict[str, Any] | None = None,
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
    source: CLI source (claude, qwen, codex, droid)
        agent_id: Optional agent ID
        workflow_name: Optional workflow to activate
        agent_name: Agent definition name used for the spawned session/run
        git_branch: Optional git branch
        prompt: Optional initial prompt
        model: Optional model override.
        is_local: Whether the spawned runtime uses a local model endpoint.
        max_agent_depth: Maximum agent depth
        task_id: Optional task ID to link to the agent
        claimed_session_id: Session that owned the task when the run was created.
        timeout_seconds: Optional timeout for the agent run in seconds.
        sandbox_enabled: Whether the spawned runtime should be recorded as sandboxed.
        requested_reasoning_effort: Optional raw reasoning effort requested by the caller.
        effective_reasoning_effort: Optional provider-normalized reasoning effort.
        reasoning_required: Whether reasoning support was required by the caller.
        reasoning_status: Resolution status for reasoning effort selection.
        reasoning_message: Optional explanation of reasoning effort resolution.
        resume_metadata_json: Optional daemon-stop resume metadata snapshot to persist
            on the created agent run. Must be a JSON-safe object with string keys
            and JSON scalar/list/dict values. Defaults to None. When present, it is
            stored as-is for later resume and is not used to build terminal env vars.

    Returns:
        PreparedSpawn with all necessary spawn configuration

    Raises:
        ValueError: If max agent depth exceeded
    """
    # Create child session config
    config = ChildSessionConfig(
        parent_session_id=parent_session_id,
        project_id=project_id,
        machine_id=machine_id,
        source=source,
        agent_id=agent_id,
        workflow_name=workflow_name,
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

    # Use provided agent_run_id or generate one.
    if agent_run_id is None:
        agent_run_id = str(uuid.uuid4())

    def bind_fresh_run(run_id: str) -> None:
        session_manager.update_terminal_pickup_metadata(
            session_id=child_session.id,
            agent_run_id=run_id,
            workflow_name=workflow_name,
        )

    return _prepare_run_for_session(
        session_manager=session_manager,
        session_id=child_session.id,
        session_depth=child_session.agent_depth,
        session_seq_num=getattr(child_session, "seq_num", None),
        parent_session_id=parent_session_id,
        project_id=project_id,
        provider=source,
        workflow_name=workflow_name,
        agent_name=agent_name,
        git_branch=git_branch,
        prompt=prompt,
        model=model,
        is_local=is_local,
        max_agent_depth=max_agent_depth,
        agent_run_id=agent_run_id,
        task_id=task_id,
        claimed_session_id=claimed_session_id,
        timeout_seconds=timeout_seconds,
        sandbox_enabled=sandbox_enabled,
        requested_reasoning_effort=requested_reasoning_effort,
        effective_reasoning_effort=effective_reasoning_effort,
        reasoning_required=reasoning_required,
        reasoning_status=reasoning_status,
        reasoning_message=reasoning_message,
        resume_metadata_json=resume_metadata_json,
        bind_run=bind_fresh_run,
    )


def prepare_terminal_resume(
    session_manager: ChildSessionManager,
    *,
    existing_session_id: str,
    original_run_id: str,
    parent_session_id: str,
    project_id: str,
    source: str,
    workflow_name: str | None,
    agent_name: str | None,
    initial_variables: dict[str, Any] | None,
    git_branch: str | None,
    prompt: str,
    model: str | None,
    is_local: bool,
    max_agent_depth: int,
    agent_run_id: str,
    task_id: str | None,
    claimed_session_id: str | None,
    timeout_seconds: float | None,
    sandbox_enabled: bool,
    requested_reasoning_effort: str | None,
    effective_reasoning_effort: str | None,
    reasoning_required: bool,
    reasoning_status: str,
    reasoning_message: str | None,
    resume_metadata_json: dict[str, Any],
) -> PreparedSpawn:
    """Prepare a successor run against an existing durable child session."""
    child_session = session_manager._storage.get(existing_session_id)
    if child_session is None:
        raise ValueError("Daemon resume child session does not exist")
    if child_session.status in {"expired", "deleted"}:
        raise ValueError("Daemon resume child session is terminal")
    if child_session.parent_session_id != parent_session_id:
        raise ValueError("Daemon resume child session belongs to another parent")
    if child_session.project_id != project_id:
        raise ValueError("Daemon resume child session belongs to another project")
    if child_session.agent_run_id != original_run_id:
        raise ValueError("Daemon resume child session is owned by another run")
    from gobby.storage.session_lifecycle import rebind_agent_run

    def bind_successor_run(run_id: str) -> None:
        rebound = rebind_agent_run(
            session_manager._storage.db,
            session_id=child_session.id,
            expected_run_id=original_run_id,
            new_run_id=run_id,
            workflow_name=workflow_name,
        )
        if not rebound:
            raise ValueError("Daemon resume session ownership changed concurrently")

    from gobby.storage.hub.protocol import SessionVariableMutation

    # Immediate + session-variable lock so the nested merge_variables (which
    # opens transaction_immediate itself) reuses this ambient transaction.
    with session_manager._storage.db.transaction_immediate(
        SessionVariableMutation(session_id=child_session.id)
    ):
        if initial_variables:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(session_manager._storage.db).merge_variables(
                child_session.id,
                initial_variables,
            )
        return _prepare_run_for_session(
            session_manager=session_manager,
            session_id=child_session.id,
            session_depth=child_session.agent_depth,
            session_seq_num=getattr(child_session, "seq_num", None),
            parent_session_id=parent_session_id,
            project_id=project_id,
            provider=source,
            workflow_name=workflow_name,
            agent_name=agent_name,
            git_branch=git_branch,
            prompt=prompt,
            model=model,
            is_local=is_local,
            max_agent_depth=max_agent_depth,
            agent_run_id=agent_run_id,
            task_id=task_id,
            claimed_session_id=claimed_session_id,
            timeout_seconds=timeout_seconds,
            sandbox_enabled=sandbox_enabled,
            requested_reasoning_effort=requested_reasoning_effort,
            effective_reasoning_effort=effective_reasoning_effort,
            reasoning_required=reasoning_required,
            reasoning_status=reasoning_status,
            reasoning_message=reasoning_message,
            resume_metadata_json=resume_metadata_json,
            bind_run=bind_successor_run,
        )


def _prepare_run_for_session(
    *,
    session_manager: ChildSessionManager,
    session_id: str,
    session_depth: int,
    session_seq_num: int | None,
    parent_session_id: str,
    project_id: str,
    provider: str,
    workflow_name: str | None,
    agent_name: str | None,
    git_branch: str | None,
    prompt: str | None,
    model: str | None,
    is_local: bool,
    max_agent_depth: int,
    agent_run_id: str,
    task_id: str | None,
    claimed_session_id: str | None,
    timeout_seconds: float | None,
    sandbox_enabled: bool,
    requested_reasoning_effort: str | None,
    effective_reasoning_effort: str | None,
    reasoning_required: bool,
    reasoning_status: str,
    reasoning_message: str | None,
    resume_metadata_json: dict[str, Any] | None,
    bind_run: Callable[[str], None],
) -> PreparedSpawn:
    """Create and bind a run, then construct its terminal identity."""
    from gobby.storage.agents import LocalAgentRunManager

    logging.getLogger("agents.spawn.prepare_terminal_spawn").debug(
        "Creating agent_run %s for child_session %s",
        agent_run_id,
        session_id,
    )
    agent_run_mgr = LocalAgentRunManager(session_manager._storage.db)
    agent_run_mgr.create(
        parent_session_id=parent_session_id,
        provider=provider,
        prompt=prompt or "",
        workflow_name=workflow_name,
        agent_name=agent_name,
        model=model,
        is_local=is_local,
        child_session_id=session_id,
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
    bind_run(agent_run_id)

    prompt_env: str | None = None
    prompt_file: str | None = None

    if prompt:
        if len(prompt) <= MAX_ENV_PROMPT_LENGTH:
            prompt_env = prompt
        else:
            prompt_file = create_prompt_file(prompt, session_id)

    env_vars = get_terminal_env_vars(
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent_run_id=agent_run_id,
        project_id=project_id,
        workflow_name=workflow_name,
        agent_depth=session_depth,
        max_agent_depth=max_agent_depth,
        prompt=prompt_env,
        prompt_file=prompt_file,
        operator_token=read_local_api_token(),
        timeout_seconds=timeout_seconds,
    )

    return PreparedSpawn(
        session_id=session_id,
        agent_run_id=agent_run_id,
        parent_session_id=parent_session_id,
        project_id=project_id,
        workflow_name=workflow_name,
        agent_depth=session_depth,
        env_vars=env_vars,
        seq_num=session_seq_num,
    )
