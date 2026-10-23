"""Build the node's typed spawn command from resolved hub inputs."""

from collections.abc import Mapping
from typing import Any

from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor_providers import render_agent_prompt_prefix
from gobby.config.tmux import TmuxConfig
from gobby.hub.node_commands import Sandbox, SpawnMonitor, SpawnPayload, SpawnWorkspace
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import SpawnGenerationEndpointResolution
from gobby.mcp_proxy.tools.spawn_agent._isolation_prepare import requested_base_branch
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager

# Active worktree and clone rows the run's session already owns on its machine.
_RESUME_WORKSPACE_SQL = """
SELECT path, branch_name FROM (
    SELECT worktree_path AS path, branch_name, updated_at, created_at
    FROM worktrees
    WHERE machine_id = %s AND agent_session_id = %s AND status = 'active'
    UNION ALL
    SELECT clone_path AS path, branch_name, updated_at, created_at
    FROM clones
    WHERE machine_id = %s AND agent_session_id = %s AND status = 'active'
) AS owned
ORDER BY updated_at DESC, created_at DESC
LIMIT 1
"""


def _resumed_workspace(db: HubDatabase, run: AgentRun) -> SpawnWorkspace | None:
    row = db.fetchone(
        _RESUME_WORKSPACE_SQL,
        (run.machine_id, run.child_session_id, run.machine_id, run.child_session_id),
    )
    if row is None:
        return None
    return SpawnWorkspace(
        mode="existing",
        path=row["path"],
        branch_name=row["branch_name"],
        base_branch=None,
    )


def _requested_workspace(
    run: AgentRun,
    request: Mapping[str, Any],
    body: AgentDefinitionBody | None,
) -> SpawnWorkspace:
    mode = request.get("checkout_mode")
    if mode is None and body is not None:
        mode = body.checkout_mode
    if mode is None or mode in ("inherit", "none"):
        return SpawnWorkspace(mode="none", path=None, branch_name=None, base_branch=None)
    base = requested_base_branch(
        request.get("base_branch"),
        body.base_branch if body is not None else None,
    )
    return SpawnWorkspace(
        mode=mode,
        path=None,
        branch_name=request.get("branch_name") or f"agent/{run.id}",
        base_branch=base,
    )


def _workspace(
    db: HubDatabase,
    run: AgentRun,
    request: Mapping[str, Any],
    body: AgentDefinitionBody | None,
    *,
    resume_existing: bool,
) -> SpawnWorkspace:
    # A resumed run reuses its own workspace unless the request names isolation.
    if resume_existing and request.get("checkout_mode") is None:
        resumed = _resumed_workspace(db, run)
        if resumed is not None:
            return resumed
    return _requested_workspace(run, request, body)


def build_spawn_payload(
    db: HubDatabase,
    *,
    run: AgentRun,
    request: Mapping[str, Any],
    endpoint: SpawnGenerationEndpointResolution,
    reasoning: SpawnReasoningResolution,
    sandbox_config: SandboxConfig,
    resume_metadata: Mapping[str, Any],
    code_index_mode: str | None,
    tmux_config: TmuxConfig,
) -> SpawnPayload:
    """Build the validated ``agent.spawn`` payload for one agent run.

    Request keys read: ``prompt``, ``project_id``, ``project_path``,
    ``terminal_id`` (required, hub-minted), ``initial_variables`` (required),
    ``agent_depth`` (0), ``max_agent_depth`` (5), ``title``, ``timeout``,
    ``droid_mode`` ("exec"), ``agent_lookup_name``, ``agent_body``,
    ``checkout_mode``, ``branch_name``, ``base_branch``.

    ``agent_body`` is the already-resolved agent; it is never re-resolved here.
    ``timeout_seconds`` 0 means no timeout. The payload is validated before any
    write; the codex context flag is merged only after validation succeeds.
    """
    if run.child_session_id is None:
        raise ValueError(f"agent run {run.id} has no child session")
    body: AgentDefinitionBody | None = request.get("agent_body")
    resume_existing = bool(resume_metadata.get("resume_existing_session"))

    timeout = request.get("timeout")
    if timeout is None:
        timeout = body.timeout if body is not None and body.timeout else 0

    prompt = request["prompt"]
    prefix = None
    if run.provider == "codex":
        prefix = render_agent_prompt_prefix(body, request["initial_variables"])
    if prefix:
        prompt = f"{prefix}\n\n{prompt}"

    payload = SpawnPayload(
        run_id=run.id,
        session_id=run.child_session_id,
        parent_session_id=run.parent_session_id,
        project_id=request["project_id"],
        terminal_id=request["terminal_id"],
        agent_depth=request.get("agent_depth", 0),
        max_agent_depth=request.get("max_agent_depth", 5),
        workflow_name=run.workflow_name,
        provider=run.provider,
        model=endpoint.model,
        prompt=prompt,
        provider_args=[],
        reasoning_effort=reasoning.effective_effort,
        sandbox=Sandbox.model_validate(sandbox_config.model_dump(mode="json")),
        timeout_seconds=int(timeout),
        title=request.get("title"),
        source_root=request["project_path"],
        workspace=_workspace(db, run, request, body, resume_existing=resume_existing),
        agent_name=request.get("agent_lookup_name") or (body.name if body is not None else None),
        api_base=endpoint.api_base,
        api_token=endpoint.api_token,
        codex_oss_provider=endpoint.codex_oss_provider,
        resume_session_id=(
            resume_metadata.get("provider_native_session_id") if resume_existing else None
        ),
        execution_mode=resume_metadata.get("execution_mode"),
        code_index_preflight=code_index_mode,
        task_id=run.task_id,
        extra_env=dict(endpoint.child_env or {}),
        codex_config_overrides=list(endpoint.codex_config_overrides),
        droid_mode=request.get("droid_mode", "exec"),
        prewarm_pre_commit_store=body.prewarm_pre_commit_store if body is not None else True,
        monitor=SpawnMonitor(
            auto_enter_agent_terminals=tmux_config.auto_enter_agent_terminals,
            auto_enter_agent_interval_seconds=tmux_config.auto_enter_agent_interval_seconds,
            auto_enter_approval_prompts=tmux_config.auto_enter_approval_prompts,
            init_timeout_seconds=tmux_config.init_timeout_seconds,
        ),
    )
    if prefix:
        SessionVariableManager(db).merge_variables(
            run.child_session_id, {"_agent_context_injected": True}
        )
    return payload
