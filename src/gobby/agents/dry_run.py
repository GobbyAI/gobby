"""
Agent spawn dry-run evaluator.

Simulates a spawn_agent call without executing, reporting what would happen
and identifying misconfigurations before any resources are allocated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.workflows.dry_run import EvaluationItem, WorkflowEvaluation

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.database import DatabaseProtocol
    from gobby.workflows.loader import WorkflowLoader
    from gobby.workflows.state_manager import WorkflowStateManager

logger = logging.getLogger(__name__)


@dataclass
class SpawnEvaluation:
    """Result of evaluating a spawn_agent dry-run."""

    can_spawn: bool
    items: list[EvaluationItem] = field(default_factory=list)

    # Agent resolution
    agent_name: str | None = None
    agent_found: bool = False
    effective_workflow: str | None = None
    effective_mode: str | None = None
    effective_isolation: str | None = None
    effective_provider: str | None = None
    branch_name: str | None = None

    # Embedded workflow evaluation
    workflow_evaluation: WorkflowEvaluation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_spawn": self.can_spawn,
            "items": [i.to_dict() for i in self.items],
            "agent_name": self.agent_name,
            "agent_found": self.agent_found,
            "effective_workflow": self.effective_workflow,
            "effective_mode": self.effective_mode,
            "effective_isolation": self.effective_isolation,
            "effective_provider": self.effective_provider,
            "branch_name": self.branch_name,
            "workflow_evaluation": self.workflow_evaluation.to_dict()
            if self.workflow_evaluation
            else None,
        }

    @property
    def errors(self) -> list[EvaluationItem]:
        return [i for i in self.items if i.level == "error"]

    @property
    def warnings(self) -> list[EvaluationItem]:
        return [i for i in self.items if i.level == "warning"]


def _load_agent_body(
    name: str,
    db: DatabaseProtocol | None,
) -> Any:
    """Load an AgentDefinitionBody from the DB by name."""
    if db is None:
        return None
    try:
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
        from gobby.workflows.definitions import AgentDefinitionBody

        manager = LocalWorkflowDefinitionManager(db)
        rows = manager.list_all(workflow_type="agent")
        for row in rows:
            if row.name == name:
                return AgentDefinitionBody.model_validate_json(row.definition_json)
    except Exception as e:
        logger.warning(f"Failed to load agent definition '{name}': {e}")
    return None


async def evaluate_spawn(
    agent: str = "default",
    workflow: str | None = None,
    task_id: str | None = None,
    isolation: str | None = None,
    mode: str | None = None,
    provider: str | None = None,
    branch_name: str | None = None,
    base_branch: str | None = None,
    parent_session_id: str | None = None,
    project_path: str | None = None,
    # Injected dependencies
    db: DatabaseProtocol | None = None,
    workflow_loader: WorkflowLoader | None = None,
    runner: AgentRunner | None = None,
    state_manager: WorkflowStateManager | None = None,
    session_manager: Any | None = None,
    git_manager: Any | None = None,
    worktree_storage: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    task_manager: Any | None = None,
    mcp_manager: MCPClientManager | None = None,
) -> SpawnEvaluation:
    """
    Evaluate a spawn_agent call without executing.

    Checks agent definition, workflow resolution, isolation config,
    and runtime environment to identify issues before spawning.
    """
    result = SpawnEvaluation(can_spawn=True, agent_name=agent)

    # ---- Layer 1: Agent Definition Resolution ----
    agent_body = _load_agent_body(agent, db)

    if agent_body is None:
        result.agent_found = False
        result.can_spawn = False
        result.items.append(
            EvaluationItem(
                layer="agent",
                level="error",
                code="AGENT_NOT_FOUND",
                message=f"Agent definition '{agent}' not found",
            )
        )
        return result

    result.agent_found = True

    # Resolve effective values
    eff_provider = provider or agent_body.provider
    eff_isolation = isolation or agent_body.isolation or "none"

    result.effective_provider = eff_provider
    result.effective_isolation = eff_isolation

    result.items.append(
        EvaluationItem(
            layer="agent",
            level="info",
            code="AGENT_RESOLVED",
            message=f"Agent '{agent}' found: provider={eff_provider}, mode={agent_body.mode}",
            detail={
                "provider": eff_provider,
                "mode": agent_body.mode,
                "isolation": eff_isolation,
                "model": agent_body.model,
                "timeout": agent_body.timeout,
                "max_turns": agent_body.max_turns,
            },
        )
    )

    # ---- Layer 2: Workflow Resolution ----
    effective_workflow = workflow or agent_body.workflows.pipeline
    eff_mode = mode or agent_body.mode
    result.effective_workflow = effective_workflow
    result.effective_mode = eff_mode

    if effective_workflow:
        result.items.append(
            EvaluationItem(
                layer="workflow_resolution",
                level="info",
                code="WORKFLOW_RESOLVED",
                message=f"Workflow resolved to '{effective_workflow}' (mode={eff_mode})",
                detail={"workflow": effective_workflow, "mode": eff_mode},
            )
        )

        # Validate workflow for agent usage
        if workflow_loader is not None:
            is_valid, error_msg = await workflow_loader.validate_workflow_for_agent(
                effective_workflow,
                project_path,
            )
            if not is_valid:
                result.can_spawn = False
                result.items.append(
                    EvaluationItem(
                        layer="workflow_resolution",
                        level="error",
                        code="WORKFLOW_INVALID_FOR_AGENT",
                        message=error_msg
                        or f"Workflow '{effective_workflow}' is not valid for agent spawning",
                    )
                )
    else:
        result.items.append(
            EvaluationItem(
                layer="workflow_resolution",
                level="info",
                code="NO_WORKFLOW",
                message="No workflow configured — agent will run without workflow enforcement",
            )
        )

    # ---- Layer 3: Isolation Resolution ----
    if eff_isolation in ("worktree", "clone"):
        storage = worktree_storage if eff_isolation == "worktree" else clone_storage
        manager_dep = git_manager if eff_isolation == "worktree" else clone_manager

        if manager_dep is None or storage is None:
            result.items.append(
                EvaluationItem(
                    layer="isolation",
                    level="warning",
                    code="ISOLATION_DEPS_MISSING",
                    message=f"{eff_isolation.title()} isolation requires dependencies",
                )
            )
        elif project_path:
            from gobby.agents.isolation import SpawnConfig, generate_branch_name

            config = SpawnConfig(
                prompt="",
                task_id=task_id,
                task_title=None,
                task_seq_num=None,
                branch_name=branch_name,
                branch_prefix=None,
                base_branch=base_branch or agent_body.base_branch,
                project_id="",
                project_path=project_path,
                provider=eff_provider,
                parent_session_id=parent_session_id or "",
            )
            computed_branch = generate_branch_name(config)
            result.branch_name = computed_branch

            try:
                from gobby.utils.project_context import get_project_context

                ctx = get_project_context()
                proj_id = ctx.get("id", "") if ctx else ""
                existing = storage.get_by_branch(proj_id, computed_branch)
                if existing:
                    result.items.append(
                        EvaluationItem(
                            layer="isolation",
                            level="info",
                            code=f"EXISTING_{eff_isolation.upper()}",
                            message=f"Existing {eff_isolation} found for branch '{computed_branch}' — will be reused",
                            detail={"branch": computed_branch, f"{eff_isolation}_id": existing.id},
                        )
                    )
            except Exception:
                logger.debug(
                    "Failed to check existing %s for branch '%s'",
                    eff_isolation,
                    computed_branch,
                    exc_info=True,
                )

    # ---- Layer 4: Runtime Environment ----
    if parent_session_id and runner is not None:
        can_spawn_result, reason, _depth = runner.can_spawn(parent_session_id)
        if not can_spawn_result:
            result.can_spawn = False
            result.items.append(
                EvaluationItem(
                    layer="runtime",
                    level="error",
                    code="SPAWN_DEPTH_EXCEEDED",
                    message=f"Cannot spawn: {reason}",
                    detail={"reason": reason},
                )
            )
        else:
            result.items.append(
                EvaluationItem(
                    layer="runtime",
                    level="info",
                    code="SPAWN_DEPTH_OK",
                    message=f"Spawn depth check passed: {reason}",
                )
            )

    # mode=self: check for existing step workflow on parent session
    if eff_mode == "self" and parent_session_id and state_manager:
        parent_state = state_manager.get_state(parent_session_id)
        if (
            parent_state
            and parent_state.workflow_name
            and parent_state.workflow_name != "__lifecycle__"
        ):
            result.items.append(
                EvaluationItem(
                    layer="runtime",
                    level="warning",
                    code="SELF_MODE_WORKFLOW_CONFLICT",
                    message=(
                        f"mode=self would activate workflow on parent session, but parent "
                        f"already has workflow '{parent_state.workflow_name}' active"
                    ),
                    detail={"parent_workflow": parent_state.workflow_name},
                )
            )

    # Terminal availability check
    if eff_mode == "terminal":
        try:
            from gobby.agents.tmux.spawner import TmuxSpawner

            spawner = TmuxSpawner()
            if not spawner.is_available():
                result.items.append(
                    EvaluationItem(
                        layer="runtime",
                        level="warning",
                        code="NO_TERMINALS_AVAILABLE",
                        message="tmux is not available — agent may fail to spawn",
                    )
                )
            else:
                result.items.append(
                    EvaluationItem(
                        layer="runtime",
                        level="info",
                        code="TERMINALS_AVAILABLE",
                        message="Available terminals: ['tmux']",
                    )
                )
        except Exception:
            logger.debug("Failed to check terminal availability", exc_info=True)

    # ---- Layer 5: Workflow Evaluation (delegates to evaluate_workflow) ----
    if effective_workflow and workflow_loader is not None:
        from gobby.workflows.dry_run import evaluate_workflow

        wf_eval = await evaluate_workflow(
            effective_workflow,
            workflow_loader,
            project_path,
            mcp_manager,
        )
        result.workflow_evaluation = wf_eval

        # Merge workflow items into top-level items
        for item in wf_eval.items:
            result.items.append(item)

        if not wf_eval.valid:
            result.can_spawn = False

    # Final validity determination
    if any(i.level == "error" for i in result.items):
        result.can_spawn = False

    return result
