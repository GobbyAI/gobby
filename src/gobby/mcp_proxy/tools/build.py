"""MCP build tool surface."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from gobby.app_context import get_app_context
from gobby.build import (
    build_clean_target,
    build_restart_target,
    build_resume,
    build_resume_target,
    build_stop,
    build_stop_target,
)
from gobby.build.options import resolve_build_isolation
from gobby.build.service import BuildOptions, build
from gobby.config.build import DeliveryMode, Isolation, StageCapOverride
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

WorkspaceBackend = Literal["worktree", "clone"]

AUTOMATION_DISABLED_MESSAGE = (
    "automation_disabled: project build automation is paused. "
    "Run `gobby build resume` to re-enable build automation."
)


def _resolve_coordinator_session_ref(coordinator: str | None) -> str | None:
    """Resolve MCP-only coordinator aliases before handing off to build service."""
    if coordinator is None:
        return None
    ref = coordinator.strip()
    if not ref:
        return None
    if ref != "current":
        return ref
    session_id = get_current_session_id()
    if not session_id:
        raise ValueError("coordinator=current requires an MCP session context")
    return session_id


def create_build_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the build tool registry."""

    registry = InternalToolRegistry(
        name="gobby-build",
        description="Build automation tools",
    )

    async def build_task(
        input_ref: str,
        profile: str | None = None,
        quick: bool = False,
        skip_stages: list[str] | None = None,
        isolation: Isolation | None = None,
        workspace_backend: WorkspaceBackend | None = None,
        clone: bool = False,
        unattended: bool | None = None,
        delivery_mode: DeliveryMode | None = None,
        delivery_target_repo: str | None = None,
        no_merge: bool = False,
        pr: str | None = None,
        stage: list[str] | None = None,
        target_branch: str | None = None,
        agent: str | None = None,
        clones_dir: str | None = None,
        cwd: str | None = None,
        reset_expansion_output: bool = False,
        max_active_agents: int | None = None,
        max_retries: int | None = None,
        planning_seed_state: Literal["drafted", "needs_review", "approved"] = "drafted",
        completed_plan_review_rounds: int = 0,
        dry_run: bool = False,
        coordinator: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Start lifecycle automation for a plan file, epic, or automated leaf task."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_task")
        resolved_isolation = resolve_build_isolation(
            isolation=isolation,
            workspace_backend=workspace_backend,
            clone=clone,
        )

        opts = BuildOptions(
            profile=profile or "default",
            profile_explicit=profile is not None,
            quick=quick,
            skip_stages=skip_stages or [],
            skip_stages_explicit=skip_stages is not None,
            isolation=resolved_isolation.isolation,
            isolation_explicit=resolved_isolation.explicit,
            unattended=unattended if unattended is not None else False,
            unattended_explicit=unattended is not None,
            delivery_mode=delivery_mode or "auto",
            delivery_mode_explicit=delivery_mode is not None,
            delivery_target_repo=delivery_target_repo,
            delivery_target_repo_explicit=delivery_target_repo is not None,
            no_merge=no_merge,
            pr=pr,
            stage_caps=_stage_caps_from_payload(stage or []),
            target_branch=target_branch,
            assigned_agent=agent,
            clones_dir=Path(clones_dir).expanduser() if clones_dir is not None else None,
            cwd=Path(cwd).expanduser() if cwd is not None else None,
            reset_expansion_output=reset_expansion_output,
            max_active_agents=max_active_agents,
            max_retries=max_retries,
            planning_seed_state=planning_seed_state,
            completed_plan_review_rounds=completed_plan_review_rounds,
            dry_run=dry_run,
            coordinator_session_ref=_resolve_coordinator_session_ref(coordinator),
            project_explicit=project_id is not None,
        )
        result = await build(
            input_ref,
            opts,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
            services=get_app_context(),
        )
        payload = asdict(result)
        if not payload.get("warnings"):
            payload.pop("warnings", None)
        dispatcher_tick = payload.get("dispatcher_tick")
        if (
            isinstance(dispatcher_tick, dict)
            and dispatcher_tick.get("reason") == "automation_disabled"
        ):
            payload["automation_disabled"] = True
            payload["message"] = AUTOMATION_DISABLED_MESSAGE
        return payload

    async def build_stop_tool(
        input_ref: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop project-wide dispatcher ticks or task-scoped automation."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_stop")
        if input_ref:
            return asdict(
                await build_stop_target(
                    input_ref,
                    db=ctx.task_manager.db,
                    project_id=resolved_project_id,
                    services=get_app_context(),
                )
            )
        return asdict(build_stop(db=ctx.task_manager.db, project_id=resolved_project_id))

    async def build_resume_tool(
        input_ref: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume project-wide dispatcher ticks or task-scoped automation."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_resume")
        if input_ref:
            return asdict(
                await build_resume_target(
                    input_ref,
                    db=ctx.task_manager.db,
                    project_id=resolved_project_id,
                    services=get_app_context(),
                )
            )
        return asdict(build_resume(db=ctx.task_manager.db, project_id=resolved_project_id))

    async def build_clean_tool(
        input_ref: str,
        dry_run: bool = False,
        force: bool = False,
        yes: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete failed build artifacts for a task ref."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_clean")
        result = await build_clean_target(
            input_ref,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
            dry_run=dry_run,
            force=force,
            yes=yes,
            services=get_app_context(),
        )
        return asdict(result)

    async def build_restart_tool(
        input_ref: str,
        dry_run: bool = False,
        force: bool = False,
        yes: bool = False,
        no_resume: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop, clean, and resume task-scoped build automation."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_restart")
        result = await build_restart_target(
            input_ref,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
            dry_run=dry_run,
            force=force,
            yes=yes,
            no_resume=no_resume,
            services=get_app_context(),
        )
        return asdict(result)

    registry.register(
        name="build_task",
        description=("Start lifecycle automation for a plan file, epic, or automated leaf task."),
        input_schema={
            "type": "object",
            "properties": {
                "input_ref": {"type": "string"},
                "profile": {"type": "string"},
                "quick": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
                "skip_stages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree", "clone"],
                },
                "workspace_backend": {
                    "type": "string",
                    "enum": ["worktree", "clone"],
                },
                "clone": {"type": "boolean", "default": False},
                "unattended": {"type": "boolean"},
                "delivery_mode": {"type": "string", "enum": ["auto", "pull_request"]},
                "delivery_target_repo": {"type": "string"},
                "no_merge": {"type": "boolean", "default": False},
                "pr": {"type": "string"},
                "stage": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Stage selector/settings, e.g. development:max_review_rounds=4",
                },
                "target_branch": {"type": "string"},
                "agent": {"type": "string"},
                "clones_dir": {"type": "string"},
                "cwd": {"type": "string"},
                "reset_expansion_output": {"type": "boolean", "default": False},
                "max_active_agents": {"type": "integer", "minimum": 1},
                "max_retries": {"type": "integer", "minimum": 0},
                "planning_seed_state": {
                    "type": "string",
                    "enum": ["drafted", "needs_review", "approved"],
                    "default": "drafted",
                },
                "completed_plan_review_rounds": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "coordinator": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=build_task,
    )

    control_project_schema = {
        "type": "object",
        "properties": {
            "input_ref": {"type": "string"},
            "project_id": {"type": "string"},
        },
    }
    target_control_properties: dict[str, dict[str, object]] = {
        "input_ref": {"type": "string"},
        "dry_run": {"type": "boolean", "default": False},
        "force": {"type": "boolean", "default": False},
        "yes": {"type": "boolean", "default": False},
        "project_id": {"type": "string"},
    }
    target_control_schema = {
        "type": "object",
        "properties": target_control_properties,
        "required": ["input_ref"],
    }
    restart_schema = {
        "type": "object",
        "properties": {
            **target_control_properties,
            "no_resume": {"type": "boolean", "default": False},
        },
        "required": ["input_ref"],
    }
    registry.register(
        name="build_stop",
        description="Stop project-wide dispatcher ticks or task-scoped automation.",
        input_schema=control_project_schema,
        func=build_stop_tool,
    )
    registry.register(
        name="build_resume",
        description="Resume project-wide dispatcher ticks or task-scoped automation.",
        input_schema=control_project_schema,
        func=build_resume_tool,
    )
    registry.register(
        name="build_clean",
        description="Delete failed build artifacts for a task ref.",
        input_schema=target_control_schema,
        func=build_clean_tool,
    )
    registry.register(
        name="build_restart",
        description="Stop, clean, and resume task-scoped build automation.",
        input_schema=restart_schema,
        func=build_restart_tool,
    )

    return registry


def _stage_caps_from_payload(payload: list[str]) -> list[StageCapOverride]:
    parsed: dict[str, dict[str, int | None]] = {}
    for raw in payload:
        stage_name, separator, settings_text = raw.partition(":")
        stage_name = stage_name.strip()
        if not stage_name:
            raise ValueError("stage name is required")
        settings = parsed.setdefault(stage_name, {})
        if not separator:
            continue
        for item in (part.strip() for part in settings_text.split(",") if part.strip()):
            key, key_separator, value_text = item.partition("=")
            if not key_separator:
                raise ValueError("stage setting must use name=value")
            key = key.strip()
            if key not in {"max_work_attempts", "max_review_rounds"}:
                raise ValueError("stage setting must be max_work_attempts or max_review_rounds")
            try:
                settings[key] = int(value_text)
            except ValueError as exc:
                raise ValueError("stage setting value must be an integer") from exc
    return [
        StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=settings.get("max_work_attempts"),
            max_review_rounds=settings.get("max_review_rounds"),
        )
        for stage_name, settings in parsed.items()
    ]


__all__ = ["StageCapOverride", "create_build_registry"]
