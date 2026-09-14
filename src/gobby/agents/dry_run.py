"""
Agent spawn dry-run evaluator.

Simulates a spawn_agent call without executing, reporting what would happen
and identifying misconfigurations before any resources are allocated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    SpawnArgumentError,
    concrete_provider,
    incompatible_spawn_model_provider,
    missing_provider_for_supplied_model,
    resolve_spawn_provider,
    spawning_session_provider,
)
from gobby.workflows.dry_run import EvaluationItem, WorkflowEvaluation

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.workflows.dry_run import MCPInventoryProtocol
    from gobby.workflows.pipeline_loader import PipelineLoader

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


def _argument_error_item(error: SpawnArgumentError) -> EvaluationItem:
    detail: dict[str, Any] = {"error_code": error.error_code}
    if error.model is not None:
        detail["model"] = error.model
    if error.provider is not None:
        detail["provider"] = error.provider
    if error.compatible_providers:
        detail["compatible_providers"] = list(error.compatible_providers)
    return EvaluationItem(
        layer="provider",
        level="error",
        code=error.error_code.upper(),
        message=error.error,
        detail=detail,
    )


async def evaluate_spawn(
    agent: str = "default",
    workflow: str | None = None,
    task_id: str | None = None,
    isolation: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    branch_name: str | None = None,
    base_branch: str | None = None,
    parent_session_id: str | None = None,
    project_path: str | None = None,
    target_project_id: str | None = None,
    # Injected dependencies
    db: HubDatabase | None = None,
    workflow_loader: PipelineLoader | None = None,
    runner: AgentRunner | None = None,
    session_manager: Any | None = None,
    git_manager: Any | None = None,
    git_manager_resolver: Callable[[str], Any | None] | None = None,
    worktree_storage: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    task_manager: Any | None = None,
    mcp_manager: MCPInventoryProtocol | None = None,
) -> SpawnEvaluation:
    """
    Evaluate a spawn_agent call without executing.

    Checks agent definition, workflow resolution, isolation config,
    provider/model compatibility, and runtime environment to identify
    issues before spawning.
    """
    result = SpawnEvaluation(can_spawn=True, agent_name=agent)
    missing_provider = missing_provider_for_supplied_model(
        explicit_provider=provider,
        model=model,
    )
    if missing_provider is not None:
        result.can_spawn = False
        result.items.append(_argument_error_item(missing_provider))
    explicit_provider = concrete_provider(provider)
    if explicit_provider is not None:
        pair_error = incompatible_spawn_model_provider(
            provider=explicit_provider,
            model=model,
        )
        if pair_error is not None:
            result.can_spawn = False
            result.items.append(_argument_error_item(pair_error))

    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context(Path(project_path)) if project_path else get_project_context()
    raw_project_id = project_ctx.get("id") if project_ctx else None
    workflow_project_id = target_project_id
    if workflow_project_id is None and isinstance(raw_project_id, str):
        workflow_project_id = raw_project_id
    context_project_path = project_ctx.get("project_path") if project_ctx else None
    resolved_project_path = (
        context_project_path if isinstance(context_project_path, str) else project_path
    )

    # ---- Layer 1: Agent Definition Resolution ----
    agent_body = None
    if db is not None:
        from gobby.workflows.agent_resolver import resolve_agent

        agent_body = resolve_agent(agent, db, project_id=workflow_project_id)

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

    # Resolve effective values. A supplied model already required an explicit
    # provider above; otherwise use explicit > agent definition > session default.
    if model is not None and model.strip() and model.strip().lower() != "inherit":
        eff_provider = explicit_provider
    else:
        try:
            eff_provider = resolve_spawn_provider(
                explicit_provider=provider,
                agent_provider=agent_body.provider,
                default_provider=spawning_session_provider(
                    session_manager,
                    caller_session_id=None,
                    parent_session_id=parent_session_id,
                ),
            )
        except ValueError as exc:
            result.can_spawn = False
            result.items.append(
                EvaluationItem(
                    layer="provider",
                    level="error",
                    code="PROVIDER_UNRESOLVED",
                    message=str(exc),
                )
            )
            eff_provider = None
    eff_isolation = isolation or agent_body.isolation or "none"

    result.effective_provider = eff_provider
    result.effective_isolation = eff_isolation

    result.items.append(
        EvaluationItem(
            layer="agent",
            level="info",
            code="AGENT_RESOLVED",
            message=f"Agent '{agent}' found: provider={eff_provider}, isolation={eff_isolation}",
            detail={
                "provider": eff_provider,
                "isolation": eff_isolation,
                "model": agent_body.model,
                "timeout": agent_body.timeout,
            },
        )
    )

    # ---- Layer 2: Workflow Resolution ----
    effective_workflow = workflow or agent_body.workflows.pipeline
    result.effective_workflow = effective_workflow

    if effective_workflow:
        result.items.append(
            EvaluationItem(
                layer="workflow_resolution",
                level="info",
                code="WORKFLOW_RESOLVED",
                message=f"Workflow resolved to '{effective_workflow}'",
                detail={"workflow": effective_workflow},
            )
        )

        # Validate workflow for agent usage
        if workflow_loader is not None:
            is_valid, error_msg = await workflow_loader.validate_pipeline_for_agent(
                effective_workflow,
                workflow_project_id,
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
                    level="warning",
                    code="WORKFLOW_VALIDATION_SKIPPED",
                    message="Workflow validation skipped because the workflow loader is unavailable",
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
        target_git_manager = git_manager
        target_clone_manager = clone_manager
        if git_manager_resolver is not None:
            if workflow_project_id is None:
                target_git_manager = None
                result.items.append(
                    EvaluationItem(
                        layer="isolation",
                        level="error",
                        code="PROJECT_CONTEXT_MISSING",
                        message="Could not resolve target project for isolation",
                    )
                )
            else:
                try:
                    target_git_manager = git_manager_resolver(workflow_project_id)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    target_git_manager = None
                    result.items.append(
                        EvaluationItem(
                            layer="isolation",
                            level="error",
                            code="GIT_MANAGER_UNAVAILABLE",
                            message=(
                                "Could not resolve Git manager for project "
                                f"'{workflow_project_id}': {exc}"
                            ),
                        )
                    )
                if target_git_manager is None and not any(
                    item.code == "GIT_MANAGER_UNAVAILABLE" for item in result.items
                ):
                    result.items.append(
                        EvaluationItem(
                            layer="isolation",
                            level="error",
                            code="GIT_MANAGER_UNAVAILABLE",
                            message=(
                                f"No Git manager available for project '{workflow_project_id}'"
                            ),
                        )
                    )

        manager_repo_path = getattr(target_git_manager, "repo_path", None)
        if git_manager_resolver is not None and manager_repo_path is not None:
            resolved_project_path = str(manager_repo_path)

        if eff_isolation == "clone" and target_git_manager is not None:
            try:
                from gobby.clones.git import CloneGitManager

                target_clone_manager = CloneGitManager(target_git_manager.repo_path)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                target_clone_manager = None
                result.items.append(
                    EvaluationItem(
                        layer="isolation",
                        level="error",
                        code="CLONE_MANAGER_UNAVAILABLE",
                        message=(
                            "Could not create clone manager for project "
                            f"'{workflow_project_id}': {exc}"
                        ),
                    )
                )

        storage = worktree_storage if eff_isolation == "worktree" else clone_storage
        manager_dep = target_git_manager if eff_isolation == "worktree" else target_clone_manager

        if manager_dep is None or storage is None:
            result.items.append(
                EvaluationItem(
                    layer="isolation",
                    level="warning",
                    code="ISOLATION_DEPS_MISSING",
                    message=f"{eff_isolation.title()} isolation requires dependencies",
                )
            )
        elif resolved_project_path and eff_provider is not None:
            from gobby.agents.isolation import SpawnConfig, generate_branch_name

            config = SpawnConfig(
                prompt="",
                task_id=task_id,
                task_title=None,
                task_seq_num=None,
                branch_name=branch_name,
                branch_prefix=None,
                base_branch=base_branch or agent_body.base_branch,
                project_id=workflow_project_id or "",
                project_path=resolved_project_path,
                provider=eff_provider,
                parent_session_id=parent_session_id or "",
            )
            computed_branch = generate_branch_name(config)
            result.branch_name = computed_branch

            try:
                # worktrees/clones.project_id is a native uuid column; binding ""
                # raises, so only check for an existing branch with a real project.
                existing = (
                    storage.get_by_branch(workflow_project_id, computed_branch)
                    if workflow_project_id
                    else None
                )
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

    # Terminal availability check (all agents use the runtime registry backend)
    try:
        from gobby.agents.tmux import get_tmux_session_manager

        if not get_tmux_session_manager().is_available():
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

    # ---- Layer 5: Pipeline Evaluation (delegates to evaluate_pipeline_definition) ----
    if effective_workflow and workflow_loader is not None:
        from gobby.workflows.dry_run import evaluate_pipeline_definition

        wf_eval = await evaluate_pipeline_definition(
            effective_workflow,
            workflow_loader,
            workflow_project_id,
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
