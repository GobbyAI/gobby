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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV
from gobby.utils.local_token import read_local_api_token

if TYPE_CHECKING:
    from gobby.storage.managed_credentials import ManagedCredential

__all__ = [
    # Result dataclasses
    "SpawnResult",
    # Base class
    "TerminalSpawnerBase",
    # Spawner (tmux-only)
    "TmuxSpawner",
    # Helpers
    "PreparedSpawn",
    "cleanup_unlaunched_spawn",
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

    managed_credential: ManagedCredential | None = None
    """Run-scoped database credential issued before provider launch."""

    prompt_file: str | None = None
    """On-disk prompt file created during preparation, if any."""


def cleanup_unlaunched_spawn(
    session_manager: ChildSessionManager,
    *,
    session_id: str | None = None,
    agent_run_id: str | None = None,
    prompt_file: str | None = None,
    managed_credential: ManagedCredential | None = None,
) -> None:
    """Idempotently tear down pre-launch spawn acquisitions.

    Deletes the child session, its variables, the agent-run row, the on-disk
    prompt file, and any typed step instance, and revokes a never-launched
    credential. Repeated calls are safe. Cleanup failures are logged with the
    surviving identifiers rather than hiding the original error.
    """
    from pathlib import Path

    survivors: list[str] = []
    database = session_manager._storage.db

    if managed_credential is not None:
        credential_manager = vars(database).get("managed_credential_manager")
        if credential_manager is not None:
            try:
                credential_manager.revoke(
                    managed_credential.managed_execution_id,
                    generation=managed_credential.credential_generation,
                    reason="prelaunch_cleanup",
                )
            except Exception as exc:
                survivors.append(f"credential:{managed_credential.managed_execution_id}:{exc}")

    if session_id is not None:
        try:
            from gobby.workflows.step_instances import AgentStepInstanceManager

            AgentStepInstanceManager(database).delete_for_session(session_id)
        except Exception as exc:
            survivors.append(f"instance:{session_id}:{exc}")

    if agent_run_id is not None:
        try:
            database.execute("DELETE FROM agent_runs WHERE id = %s", (agent_run_id,))
        except Exception as exc:
            survivors.append(f"agent_run:{agent_run_id}:{exc}")

    if session_id is not None:
        try:
            database.execute(
                "DELETE FROM session_variables WHERE session_id = %s",
                (session_id,),
            )
        except Exception as exc:
            survivors.append(f"session_variables:{session_id}:{exc}")
        try:
            session_manager._storage.delete(session_id)
        except Exception as exc:
            survivors.append(f"session:{session_id}:{exc}")

    if prompt_file:
        try:
            Path(prompt_file).unlink(missing_ok=True)
        except Exception as exc:
            survivors.append(f"prompt_file:{prompt_file}:{exc}")

    if survivors:
        logger.error(
            "Pre-launch spawn cleanup left surviving state: %s",
            "; ".join(survivors),
        )


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

    child_session_id: str | None = None
    resolved_run_id = agent_run_id or str(uuid.uuid4())
    prompt_file: str | None = None
    prepared: PreparedSpawn | None = None
    try:
        child_session = session_manager.create_child_session(config)
        child_session_id = child_session.id

        if initial_variables:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(session_manager._storage.db).merge_variables(
                child_session.id, initial_variables
            )

        def bind_fresh_run(run_id: str) -> None:
            session_manager.update_terminal_pickup_metadata(
                session_id=child_session.id,
                agent_run_id=run_id,
                workflow_name=workflow_name,
            )

        prepared = _prepare_run_for_session(
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
            agent_run_id=resolved_run_id,
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
        prompt_file = prepared.prompt_file
        return _issue_prelaunch_credential(
            session_manager,
            prepared,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        cleanup_unlaunched_spawn(
            session_manager,
            session_id=child_session_id or (prepared.session_id if prepared else None),
            agent_run_id=prepared.agent_run_id if prepared else resolved_run_id,
            prompt_file=prompt_file or (prepared.prompt_file if prepared else None),
            managed_credential=prepared.managed_credential if prepared else None,
        )
        raise


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
        prepared = _prepare_run_for_session(
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
    return _issue_prelaunch_credential(
        session_manager,
        prepared,
        timeout_seconds=timeout_seconds,
    )


def _issue_prelaunch_credential(
    session_manager: ChildSessionManager,
    prepared: PreparedSpawn,
    *,
    timeout_seconds: float | None,
) -> PreparedSpawn:
    """Issue a scoped role after run commit and before provider launch."""
    database = session_manager._storage.db
    credential_manager = vars(database).get("managed_credential_manager")
    if credential_manager is None:
        return prepared
    lifetime_seconds = 3540.0 if timeout_seconds is None else timeout_seconds
    lifetime_seconds = max(lifetime_seconds + 300.0, 1.0)
    credential = credential_manager.issue(
        managed_execution_id=uuid.UUID(prepared.agent_run_id),
        owner_kind="agent_run",
        session_id=uuid.UUID(prepared.session_id),
        agent_run_id=uuid.UUID(prepared.agent_run_id),
        expires_at=datetime.now(UTC) + timedelta(seconds=lifetime_seconds),
    )
    prepared.env_vars[MANAGED_EXECUTION_BOOTSTRAP_ENV] = str(credential.bootstrap_path)
    prepared.managed_credential = credential
    return prepared


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
        prompt_file=prompt_file,
    )
