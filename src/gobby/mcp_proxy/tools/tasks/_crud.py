"""CRUD operations for task management.

Provides core task operations: create, get, update, list, and tree building.
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.mcp_proxy.tools.tasks._claim_activity import confirm_claiming_session_activity
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._formatters import (
    dependency_payload,
    task_discovery_payload,
    task_summary_payload,
)
from gobby.mcp_proxy.tools.tasks._live_session_label import live_session_label_change_error
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.task_dependencies import DependencyCycleError
from gobby.storage.tasks import TASK_TYPE_CHOICES, VALID_CATEGORIES, TaskNotFoundError
from gobby.tasks.categories import IMPLEMENTATION_DOMAINS
from gobby.tasks.criteria_contract import TaskCriteriaError, require_validation_criteria
from gobby.tasks.isolation import validate_task_isolation_artifacts
from gobby.workflows.claimed_task_skills import build_claimed_task_skill_state

logger = logging.getLogger(__name__)
TASK_CATEGORY_ENUM = tuple(sorted(VALID_CATEGORIES))
IMPLEMENTATION_DOMAIN_ENUM = tuple(sorted(IMPLEMENTATION_DOMAINS))
TASK_TYPE_ENUM = TASK_TYPE_CHOICES


def _task_invariant_error(
    task_type: str,
    category: str | None,
    validation_criteria: str | None,
    implementation_domain: str | None,
) -> str | None:
    """Return the task invariant error for the effective task state."""
    try:
        require_validation_criteria(task_type, validation_criteria)
    except TaskCriteriaError as exc:
        return str(exc)
    if category == "code" and implementation_domain is None:
        return "Code tasks require implementation_domain ('backend', 'frontend', or 'fullstack')."
    return None


def create_crud_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with task CRUD tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with CRUD tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-crud",
        description="Task CRUD operations",
    )

    def create_task(
        title: str,
        category: str,
        description: str | None = None,
        priority: int = 2,
        task_type: str = "task",
        parent_task_id: str | None = None,
        blocks: list[str] | None = None,
        depends_on: list[str] | None = None,
        labels: list[str] | None = None,
        validation_criteria: str | None = None,
        implementation_domain: str | None = None,
        claim: bool = False,
        project: str | None = None,
        start_date: str | None = None,
        due_date: str | None = None,
        additional_skills: list[str] | None = None,
        affected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a single task in the current project.

        This tool creates exactly ONE task. Auto-decomposition of multi-step
        descriptions is disabled. Use expand_task for complex decompositions.

        Args:
            title: Task title
            description: Detailed description
            priority: Priority level (1=High, 2=Medium, 3=Low)
            task_type: Task type
            parent_task_id: Optional parent task ID
            blocks: List of task IDs that this new task blocks
            depends_on: List of task IDs that this new task depends on (must complete first)
            labels: List of labels
            category: Task domain category (test, code, document, research, config, manual)
            validation_criteria: Acceptance criteria for validating completion.
                Required for every task_type except "epic".
            implementation_domain: Required code task implementation domain.
            claim: If True, auto-claim the task for the current session.
            additional_skills: Optional skills required to work on the task.
            affected_files: Optional file paths this task is expected to touch.

        Returns:
            Created task dict with id (minimal) or full task details based on config.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"error": "No session context available. Ensure session_id is set."}

        # Resolve session_id — needed for project resolution and DB insert
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except Exception as e:
            logger.warning("Cannot resolve session %s for task creation", session_id, exc_info=True)
            return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Resolve project: explicit parameter or the authoritative session project.
        if project:
            try:
                resolved = ctx.resolve_project_filter(project)
            except ValueError as e:
                return {"error": str(e)}
            project_id: str = resolved or PERSONAL_PROJECT_ID
        else:
            try:
                project_id = ctx.resolve_project_from_session(session_id)
            except Exception as e:
                logger.warning(
                    "Cannot resolve project for session %s during task creation",
                    session_id,
                    exc_info=True,
                )
                return {"error": f"Cannot resolve project for session '{session_id}': {e}"}

        label_error = live_session_label_change_error(
            ctx,
            (),
            labels,
            session_id=resolved_session_id,
        )
        if label_error:
            return {"error": label_error}

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(
                    ctx.task_manager, parent_task_id, project_id
                )
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}"}

        invariant_error = _task_invariant_error(
            task_type,
            category,
            validation_criteria,
            implementation_domain,
        )
        if invariant_error:
            return {"error": invariant_error}

        # Create task
        create_result = ctx.task_manager.create_task_with_decomposition(
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            parent_task_id=parent_task_id,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            implementation_domain=implementation_domain,
            additional_skills=additional_skills,
            created_in_session_id=resolved_session_id,
        )

        task = ctx.task_manager.get_task(create_result["task"]["id"])

        if affected_files:
            TaskAffectedFileManager(ctx.task_manager.db).set_files(
                task.id, affected_files, "manual"
            )

        # Set scheduling fields if provided (post-create update)
        schedule_kwargs: dict[str, Any] = {}
        if start_date is not None:
            schedule_kwargs["start_date"] = start_date
        if due_date is not None:
            schedule_kwargs["due_date"] = due_date
        if schedule_kwargs:
            updated = ctx.task_manager.update_task(task.id, **schedule_kwargs)
            if updated:
                task = updated

        # Link task to session (best-effort) - tracks which session created the task
        try:
            ctx.session_task_manager.link_task(resolved_session_id, task.id, "created")
        except Exception as e:
            logger.warning(
                "Failed to link task %s to session %s: %s", task.id, resolved_session_id, e
            )

        # Auto-claim if requested.
        claim_warning: str | None = None
        if claim:
            # Block cross-project claiming
            try:
                claim_session = ctx.session_manager.get(resolved_session_id)
            except Exception:
                claim_session = None
            if claim_session and project_id != claim_session.project_id:
                logger.info(
                    "Skipping auto-claim for task %s: task project %s != session project %s",
                    task.id,
                    project_id,
                    claim_session.project_id,
                )
                claim = False
                claim_warning = "claim=true ignored: cannot claim a task in a different project"
            elif not confirm_claiming_session_activity(
                ctx,
                resolved_session_id,
                claim_session,
            ):
                logger.warning(
                    "Skipping auto-claim for task %s: session %s could not be marked active",
                    task.id,
                    resolved_session_id,
                )
                claim = False
                claim_warning = "claim=true ignored: current session could not be marked active"

        if claim:
            updated_task = ctx.task_manager.claim_task(task.id, resolved_session_id)
            if updated_task is None:
                logger.warning("Failed to auto-claim task %s: update_task returned None", task.id)
            else:
                task = updated_task
                # Link task to session with "claimed" action (best-effort)
                try:
                    ctx.session_task_manager.link_task(resolved_session_id, task.id, "claimed")
                except Exception as e:
                    logger.warning("Failed to link claimed task %s: %s", task.id, e)
                    pass

            # Set session variables for Claude Code (CC doesn't include tool results in PostToolUse)
            # This mirrors claim_task behavior in _lifecycle.py
            try:
                from gobby.workflows.task_claim_state import add_claimed_task

                session_vars = ctx.session_var_manager.get_variables(resolved_session_id)
                ref = f"#{task.seq_num}" if task.seq_num else task.id
                merge_dict = add_claimed_task(session_vars, task.id, ref)
                current_vars = {**session_vars, **merge_dict}
                merge_dict.update(build_claimed_task_skill_state(current_vars, ctx.task_manager))
                ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
            except Exception as e:
                logger.debug("Best-effort session variable update failed: %s", e)

            try:
                from gobby.sessions.title_lifecycle import update_title_for_claim

                update_title_for_claim(ctx.session_manager, resolved_session_id, task)
            except Exception as e:
                logger.warning("Failed to update session title after claiming %s: %s", task.id, e)

        # Handle 'blocks' argument if provided (syntactic sugar)
        # Collect errors consistently with depends_on handling below
        dependency_errors: list[str] = []
        if blocks:
            for blocked_id in blocks:
                try:
                    resolved_blocked = resolve_task_id_for_mcp(
                        ctx.task_manager, blocked_id, project_id
                    )
                    ctx.dep_manager.add_dependency(resolved_blocked, task.id, "blocks")
                except TaskNotFoundError:
                    dependency_errors.append(f"Task '{blocked_id}' not found (blocks)")
                except ValueError as e:
                    dependency_errors.append(f"Invalid ref '{blocked_id}' (blocks): {e}")
                except DependencyCycleError:
                    dependency_errors.append(f"Cycle detected for '{blocked_id}' (blocks)")

        # Handle 'depends_on' argument if provided
        # The new task depends on resolved_blocker, meaning resolved_blocker blocks the new task
        if depends_on:
            for blocker_ref in depends_on:
                try:
                    resolved_blocker = resolve_task_id_for_mcp(
                        ctx.task_manager, blocker_ref, project_id
                    )
                    ctx.dep_manager.add_dependency(task.id, resolved_blocker, "blocks")
                except TaskNotFoundError:
                    dependency_errors.append(f"Task '{blocker_ref}' not found")
                except ValueError as e:
                    dependency_errors.append(f"Invalid ref '{blocker_ref}': {e}")
                except DependencyCycleError:
                    dependency_errors.append(f"Cycle detected for '{blocker_ref}'")

        # Return minimal or full result based on config
        if ctx.show_result_on_create:
            result = task.to_dict()
        else:
            result = {
                "id": task.id,
                "seq_num": task.seq_num,
                "ref": f"#{task.seq_num}",
            }

        if claim_warning:
            result["warning"] = claim_warning

        # Include dependency errors if any
        if dependency_errors:
            result["dependency_errors"] = dependency_errors
            existing_warning = result.get("warning", "")
            dep_warning = f"Task created but {len(dependency_errors)} dependency(s) failed"
            result["warning"] = (
                f"{existing_warning}; {dep_warning}" if existing_warning else dep_warning
            )

        return result

    registry.register(
        name="create_task",
        description="Create a new task in the current project.",
        brief="Create a new task. Requires: title, session_id, category, validation_criteria (every task_type except 'epic')",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {
                    "type": "string",
                    "description": "Detailed description",
                    "default": None,
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority level (1=High, 2=Medium, 3=Low)",
                    "default": 2,
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type",
                    "enum": list(TASK_TYPE_ENUM),
                    "default": "task",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Parent task reference: #N, N (seq_num), path (1.2.3), or UUID",
                    "default": None,
                },
                "blocks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs that this new task blocks (optional)",
                    "default": None,
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tasks this new task depends on (must complete first): #N, N, path, or UUID",
                    "default": None,
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of labels (optional)",
                    "default": None,
                },
                "category": {
                    "type": "string",
                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
                    "enum": list(TASK_CATEGORY_ENUM),
                },
                "validation_criteria": {
                    "type": "string",
                    "description": "Acceptance criteria for task completion. REQUIRED for every task_type except 'epic' — creation fails without it, whatever the category. Describe what 'done' looks like — validate_task checks the diff against this.",
                    "default": None,
                },
                "implementation_domain": {
                    "type": "string",
                    "enum": list(IMPLEMENTATION_DOMAIN_ENUM),
                    "description": "Required for category='code'. Routes implementation to backend, frontend, or fullstack developer agents.",
                    "default": None,
                },
                "claim": {
                    "type": "boolean",
                    "description": "If true, auto-claim the task for the current session. Default: false.",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Target project name or UUID (e.g., '_personal'). Defaults to current project context.",
                    "default": None,
                },
                "start_date": {
                    "type": "string",
                    "description": "Planned start date (ISO 8601, e.g. '2025-03-01'). Optional.",
                    "default": None,
                },
                "due_date": {
                    "type": "string",
                    "description": "Expected completion date (ISO 8601, e.g. '2025-03-15'). Optional.",
                    "default": None,
                },
                "additional_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional skills required for working on this task.",
                    "default": None,
                },
                "affected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Expected file paths this task will create or modify.",
                    "default": None,
                },
            },
            "required": ["title", "category"],
        },
        func=create_task,
    )

    def get_task(task_id: str, brief: bool = True) -> dict[str, Any]:
        """Get task details including dependencies."""
        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e), "found": False}
        except ValueError as e:
            return {"error": str(e), "found": False}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found", "found": False}

        # Enrich with dependency info
        blockers = ctx.dep_manager.get_blockers(resolved_id)
        blocking = ctx.dep_manager.get_blocking(resolved_id)

        if brief:

            def _dep_summary(dep: Any, linked_task_id: str) -> dict[str, Any]:
                linked = ctx.task_manager.get_task(linked_task_id)
                return dependency_payload(dep, linked_task_id, linked)

            dependencies = {
                "blocked_by": [_dep_summary(b, b.depends_on) for b in blockers],
                "blocking": [_dep_summary(b, b.task_id) for b in blocking],
            }
            return task_summary_payload(task, dependencies)

        result: dict[str, Any] = task.to_dict()
        result["dependencies"] = {
            "blocked_by": [b.to_dict() for b in blockers],
            "blocking": [b.to_dict() for b in blocking],
        }

        return result

    registry.register(
        name="get_task",
        description="Get task details including dependencies. Task ID can be #N (e.g., #1), path (e.g., 1.2.3), or UUID. Returns brief format by default; set brief=false for full details including description.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "brief": {
                    "type": "boolean",
                    "description": "If true (default), return compact format (~18 fields). If false, return full details (~33 fields including description, validation, integration).",
                    "default": True,
                },
            },
            "required": ["task_id"],
        },
        func=get_task,
    )

    def update_task(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        labels: list[str] | None = None,
        validation_criteria: str | None = None,
        parent_task_id: str | None = None,
        category: str | None = None,
        task_type: str | None = None,
        start_date: str | None = None,
        due_date: str | None = None,
        allow_automation: bool | None = None,
        isolation: str | None = None,
        assigned_agent: str | None = None,
        implementation_domain: str | None = None,
        additional_skills: list[str] | None = None,
        affected_files: list[str] | None = None,
        escalation_reason: str | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        current_task = ctx.task_manager.get_task(resolved_id)
        if current_task is None:
            return {"error": f"Task {task_id} not found"}
        denied = require_claim_authority(ctx.task_manager, current_task, "update_task")
        if denied:
            return denied
        if escalation_reason is not None and not current_task.is_escalated:
            return {"error": "Cannot update escalation_reason for a task that is not escalated."}
        if labels is not None:
            label_error = live_session_label_change_error(
                ctx,
                getattr(current_task, "labels", ()),
                labels,
            )
            if label_error:
                return {"error": label_error}

        effective_category = category if category is not None else current_task.category
        effective_task_type = task_type if task_type is not None else current_task.task_type
        effective_validation_criteria = (
            validation_criteria
            if validation_criteria is not None
            else current_task.validation_criteria
        )
        effective_implementation_domain = (
            implementation_domain
            if implementation_domain is not None
            else current_task.implementation_domain
        )
        invariant_error = _task_invariant_error(
            effective_task_type,
            effective_category,
            effective_validation_criteria,
            effective_implementation_domain,
        )
        if invariant_error:
            return {"error": invariant_error}

        # Build kwargs only for non-None values to avoid overwriting with NULL
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if priority is not None:
            kwargs["priority"] = priority
        if labels is not None:
            kwargs["labels"] = labels
        if validation_criteria is not None:
            kwargs["validation_criteria"] = validation_criteria
        if parent_task_id is not None:
            # Empty string means "clear parent" - convert to None for storage layer
            # Also resolve parent_task_id if it's a reference format
            if parent_task_id:
                try:
                    resolved_parent = resolve_task_id_for_mcp(ctx.task_manager, parent_task_id)
                    kwargs["parent_task_id"] = resolved_parent
                except (TaskNotFoundError, ValueError) as e:
                    logger.warning("Invalid parent_task_id '%s': %s", parent_task_id, e)
                    return {"error": f"Invalid parent_task_id '{parent_task_id}': {e}"}
            else:
                kwargs["parent_task_id"] = None
        if category is not None:
            kwargs["category"] = category
        if task_type is not None:
            kwargs["task_type"] = task_type
        if start_date is not None:
            kwargs["start_date"] = start_date
        if due_date is not None:
            kwargs["due_date"] = due_date
        if allow_automation is not None:
            kwargs["allow_automation"] = allow_automation
        if isolation is not None:
            try:
                kwargs["isolation"] = validate_task_isolation_artifacts(
                    ctx.task_manager, resolved_id, isolation
                )
            except ValueError as e:
                return {"error": str(e)}
        if assigned_agent is not None:
            kwargs["assigned_agent"] = assigned_agent
        if implementation_domain is not None:
            kwargs["implementation_domain"] = implementation_domain
        if additional_skills is not None:
            kwargs["additional_skills"] = additional_skills
        if affected_files is not None:
            kwargs["affected_files"] = affected_files
        if escalation_reason is not None:
            kwargs["escalation_reason"] = escalation_reason

        try:
            task = ctx.task_manager.update_task(resolved_id, **kwargs)
        except ValueError as e:
            return {"error": str(e)}
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {}

    registry.register(
        name="update_task",
        description="Update task fields.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "title": {"type": "string", "description": "New title", "default": None},
                "description": {
                    "type": "string",
                    "description": "New description",
                    "default": None,
                },
                "priority": {"type": "integer", "description": "New priority", "default": None},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New labels list",
                    "default": None,
                },
                "validation_criteria": {
                    "type": "string",
                    "description": "Acceptance criteria for validating task completion. Cannot be cleared on any task_type except 'epic'.",
                    "default": None,
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Parent task reference: #N, N (seq_num), path (1.2.3), or UUID. Empty string clears parent.",
                    "default": None,
                },
                "category": {
                    "type": "string",
                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
                    "enum": list(TASK_CATEGORY_ENUM),
                    "default": None,
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type",
                    "enum": list(TASK_TYPE_ENUM),
                    "default": None,
                },
                "start_date": {
                    "type": "string",
                    "description": "Planned start date (ISO 8601, e.g. '2025-03-01'). Optional.",
                    "default": None,
                },
                "due_date": {
                    "type": "string",
                    "description": "Expected completion date (ISO 8601, e.g. '2025-03-15'). Optional.",
                    "default": None,
                },
                "allow_automation": {
                    "type": "boolean",
                    "description": "Enable or disable dispatcher automation for this task.",
                    "default": None,
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree", "clone"],
                    "description": "Automation isolation mode for future dispatch.",
                    "default": None,
                },
                "assigned_agent": {
                    "type": "string",
                    "description": "Agent name to assign this task to (e.g. 'backend-developer'). Routes leaf work in dispatch.",
                    "default": None,
                },
                "implementation_domain": {
                    "type": "string",
                    "enum": list(IMPLEMENTATION_DOMAIN_ENUM),
                    "description": "Code task implementation domain. Expansion sets this; dispatch derives the developer agent from it unless manually overridden.",
                    "default": None,
                },
                "additional_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill names to load alongside the assigned agent's defaults (e.g. ['tech-writer']).",
                    "default": None,
                },
                "affected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replacement declared file scope. An empty array clears it.",
                },
                "escalation_reason": {
                    "type": "string",
                    "description": "New reason for a currently escalated task. The escalation timestamp is preserved.",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=update_task,
    )

    def list_tasks(
        current_stage_state: str | list[str] | None = None,
        priority: int | None = None,
        task_type: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        limit: int = 50,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """List tasks with optional filters."""
        try:
            project_id = ctx.resolve_project_filter(project, all_projects)
        except ValueError as e:
            return {"error": str(e), "tasks": [], "count": 0}

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(
                    ctx.task_manager, parent_task_id, project_id
                )
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}", "tasks": [], "count": 0}

        # Handle comma-separated current-stage state strings.
        current_stage_filter: str | list[str] | None = current_stage_state
        if isinstance(current_stage_state, str) and "," in current_stage_state:
            current_stage_filter = [s.strip() for s in current_stage_state.split(",")]

        tasks = ctx.task_manager.list_tasks(
            current_stage_state=current_stage_filter,
            priority=priority,
            task_type=task_type,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            limit=limit,
            project_id=project_id,
        )
        return {"tasks": [task_discovery_payload(t) for t in tasks], "count": len(tasks)}

    registry.register(
        name="list_tasks",
        description="List tasks with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "current_stage_state": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Filter by current stage state. Can be a single state, array of states, or comma-separated string.",
                    "default": None,
                },
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority",
                    "default": None,
                },
                "task_type": {
                    "type": "string",
                    "description": "Filter by task type",
                    "default": None,
                },
                "label": {
                    "type": "string",
                    "description": "Filter by label presence",
                    "default": None,
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task: #N, N (seq_num), path (1.2.3), or UUID",
                    "default": None,
                },
                "title_like": {
                    "type": "string",
                    "description": "Filter by title (fuzzy match)",
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of tasks to return",
                    "default": 50,
                },
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, list tasks from all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
        },
        func=list_tasks,
    )

    return registry


def build_task_tree(
    ctx: RegistryContext,
    tree: dict[str, Any],
    session_id: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Create an entire task tree in one call.

    Creates tasks with parent-child relationships and wires dependencies
    based on `depends_on` title references within siblings.

    This is an internal helper function, NOT registered as an MCP tool.
    Agents should use expand_task with iterative mode for tree expansion.

    Args:
        ctx: Registry context
        tree: JSON tree structure with title, task_type, children, depends_on
        session_id: Your session ID for tracking (REQUIRED)

    Returns:
        Dict with success status, tasks_created count, epic_ref, task_refs

    Example tree:
        {
            "title": "Epic Title",
            "task_type": "epic",
            "children": [
                {
                    "title": "Phase 1",
                    "children": [
                        {"title": "Task A", "category": "code"},
                        {"title": "Task B", "category": "code", "depends_on": ["Task A"]}
                    ]
                }
            ]
        }
    """
    from gobby.tasks.tree_builder import TaskTreeBuilder

    # Resolve project: explicit param > session (authoritative).
    if project:
        try:
            resolved = ctx.resolve_project_filter(project)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        project_id: str = resolved or PERSONAL_PROJECT_ID
    else:
        try:
            project_id = ctx.resolve_project_from_session(session_id)
        except Exception as e:
            logger.warning(
                "Cannot resolve project for session %s during task-tree creation",
                session_id,
                exc_info=True,
            )
            return {
                "success": False,
                "error": f"Cannot resolve project for session '{session_id}': {e}",
                "tasks_created": 0,
                "task_refs": [],
            }

    # Build the tree
    builder = TaskTreeBuilder(
        task_manager=ctx.task_manager,
        project_id=project_id,
        session_id=session_id,
    )
    result = builder.build(tree)

    response: dict[str, Any] = {
        "success": len(result.errors) == 0,
        "tasks_created": result.tasks_created,
        "epic_ref": result.epic_ref,
        "task_refs": result.task_refs,
    }
    if result.errors:
        response["errors"] = result.errors

    return response
