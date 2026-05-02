"""Mutating MCP tools for task stage manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks._stage_states import (
    IllegalManifestMutationError,
    StageManifestSpec,
)
from gobby.storage.tasks._stage_views import stage_state_operation_view
from gobby.utils.session_context import get_current_session_id


def _resolve_task(ctx: RegistryContext, task_id: str) -> str:
    return resolve_task_id_for_mcp(ctx.task_manager, task_id)


def _session_id(ctx: RegistryContext) -> str | None:
    session_ref = get_current_session_id()
    if not session_ref:
        return None
    try:
        return ctx.resolve_session_id(session_ref)
    except Exception:
        return session_ref


def _manifest_error(error: IllegalManifestMutationError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "illegal_manifest_mutation",
        "reason": error.reason,
        "payload": {
            "task_id": error.task_id,
            "target_stage_name": error.target_stage_name,
            "target_position": error.target_position,
            "current_stage_name": error.current_stage_name,
            "current_stage_state": error.current_stage_state,
            "mutation": error.mutation,
            "reason": error.reason,
        },
    }


def _write_artifacts(ctx: RegistryContext, task_id: str, fields: dict[str, Any]) -> None:
    now = datetime.now(UTC).isoformat()
    columns = ["task_id", *fields, "updated_at"]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in [*fields, "updated_at"])
    with ctx.task_manager.db.transaction() as conn:
        conn.execute(
            f"""
            INSERT INTO task_artifacts ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(task_id) DO UPDATE SET {updates}
            """,  # nosec B608 - columns are fixed by tool implementations.
            (task_id, *fields.values(), now),
        )


def _read_artifact(ctx: RegistryContext, task_id: str, field: str) -> Any:
    row = ctx.task_manager.db.fetchone(
        f"SELECT {field} FROM task_artifacts WHERE task_id = ?",  # nosec B608
        (task_id,),
    )
    return row[field] if row is not None else None


def create_stage_ops_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create mutating task stage manifest tools for gobby-tasks-ops."""

    registry = InternalToolRegistry(
        name="gobby-tasks-stage-ops",
        description="Mutating task stage manifest tools",
    )

    def start_stage(task_id: str, stage_name: str, notes: str | None = None) -> dict[str, Any]:
        """Transition a ready stage to in_progress."""
        resolved_id = _resolve_task(ctx, task_id)
        stage = ctx.task_manager.stage_states.start_stage(
            resolved_id,
            stage_name,
            by_session_id=_session_id(ctx),
            notes=notes,
        )
        return {"ok": True, "task_id": resolved_id, "stage": stage_state_operation_view(stage)}

    registry.register(
        name="start_stage",
        description="Transition a ready stage to in_progress.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=start_stage,
    )

    def complete_stage(
        task_id: str,
        stage_name: str,
        commit_sha: str | None = None,
        artifact_updates: dict[str, str] | None = None,
        validation_override_reason: str | None = None,
    ) -> dict[str, Any]:
        """Complete a stage according to its review policy."""
        resolved_id = _resolve_task(ctx, task_id)
        stage = ctx.task_manager.stage_states.complete_stage(
            resolved_id,
            stage_name,
            by_session_id=_session_id(ctx),
            commit_sha=commit_sha,
            artifact_updates=artifact_updates,
            validation_override_reason=validation_override_reason,
        )
        return {"ok": True, "task_id": resolved_id, "stage": stage_state_operation_view(stage)}

    registry.register(
        name="complete_stage",
        description="Complete a stage according to its review policy.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "commit_sha": {"type": ["string", "null"]},
                "artifact_updates": {"type": ["object", "null"]},
                "validation_override_reason": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=complete_stage,
    )

    def fail_stage(
        task_id: str,
        stage_name: str,
        reason: str,
        needs_human: bool = False,
    ) -> dict[str, Any]:
        """Return a failed in-progress stage to ready or escalate after caps."""
        resolved_id = _resolve_task(ctx, task_id)
        stage = ctx.task_manager.stage_states.fail_stage(
            resolved_id,
            stage_name,
            reason=reason,
            needs_human=needs_human,
            by_session_id=_session_id(ctx),
        )
        return {"ok": True, "task_id": resolved_id, "stage": stage_state_operation_view(stage)}

    registry.register(
        name="fail_stage",
        description="Return a failed in-progress stage to ready or escalate after caps.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "reason": {"type": "string"},
                "needs_human": {"type": "boolean"},
            },
            "required": ["task_id", "stage_name", "reason"],
        },
        output_schema={"type": "object"},
        func=fail_stage,
    )

    def add_stage(task_id: str, stage_name: str, position: int) -> dict[str, Any]:
        """Insert a future ready stage into a task manifest."""
        resolved_id = _resolve_task(ctx, task_id)
        registry_entry = ctx.task_manager.stages_registry.get(stage_name)
        if registry_entry is None:
            raise ValueError(f"Unknown stage '{stage_name}'")
        spec = StageManifestSpec(
            stage_name=stage_name,
            position=position,
            max_work_attempts=registry_entry.default_max_work_attempts,
            max_review_rounds=registry_entry.default_max_review_rounds,
        )
        try:
            stage = ctx.task_manager.stage_states.add_stage(
                resolved_id,
                spec,
                by_session_id=_session_id(ctx),
            )
        except IllegalManifestMutationError as error:
            return _manifest_error(error)
        return {"ok": True, "task_id": resolved_id, "stage": stage_state_operation_view(stage)}

    registry.register(
        name="add_stage",
        description="Insert a future ready stage into a task manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "position": {"type": "integer"},
            },
            "required": ["task_id", "stage_name", "position"],
        },
        output_schema={"type": "object"},
        func=add_stage,
    )

    def remove_stage(task_id: str, stage_name: str) -> dict[str, Any]:
        """Remove a future ready stage from a task manifest."""
        resolved_id = _resolve_task(ctx, task_id)
        try:
            ctx.task_manager.stage_states.remove_stage(
                resolved_id,
                stage_name,
                by_session_id=_session_id(ctx),
            )
        except IllegalManifestMutationError as error:
            return _manifest_error(error)
        return {"ok": True, "task_id": resolved_id, "removed_stage": stage_name}

    registry.register(
        name="remove_stage",
        description="Remove a future ready stage from a task manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=remove_stage,
    )

    def record_pr_verdict(
        task_id: str,
        verdict: Literal["approved", "rejected", "needs_changes"],
        findings: str,
        report_ref: str | None = None,
    ) -> dict[str, Any]:
        """Persist PR verdict artifacts and advance the pr review state."""
        resolved_id = _resolve_task(ctx, task_id)
        payload = {"verdict": verdict, "findings": findings, "report_ref": report_ref}
        _write_artifacts(
            ctx,
            resolved_id,
            {
                "structured_pr_verdict": json.dumps(payload, sort_keys=True),
                "pr_review_report": report_ref or findings,
            },
        )
        if verdict == "approved":
            stage = ctx.task_manager.stage_states.approve_review(
                resolved_id,
                "pr",
                by_session_id=_session_id(ctx),
                notes=findings,
            )
        else:
            stage = ctx.task_manager.stage_states.reject_review(
                resolved_id,
                "pr",
                reason=findings,
                by_session_id=_session_id(ctx),
                notes=findings,
            )
        return {"ok": True, "task_id": resolved_id, "stage": stage_state_operation_view(stage)}

    registry.register(
        name="record_pr_verdict",
        description="Persist PR verdict artifacts and advance the pr review state.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["approved", "rejected", "needs_changes"]},
                "findings": {"type": "string"},
                "report_ref": {"type": ["string", "null"]},
            },
            "required": ["task_id", "verdict", "findings"],
        },
        output_schema={"type": "object"},
        func=record_pr_verdict,
    )

    def record_pr_opened(
        task_id: str,
        pr_url: str,
        github_pr_number: int | None = None,
    ) -> dict[str, Any]:
        """Persist PR metadata without changing pr stage state."""
        resolved_id = _resolve_task(ctx, task_id)
        existing_url = _read_artifact(ctx, resolved_id, "pr_url")
        if existing_url != pr_url:
            _write_artifacts(ctx, resolved_id, {"pr_url": pr_url})
        if github_pr_number is not None:
            ctx.task_manager.update_task(resolved_id, github_pr_number=github_pr_number)
        stage = ctx.task_manager.stage_states.get(resolved_id, "pr")
        return {
            "ok": True,
            "task_id": resolved_id,
            "pr_url": pr_url,
            "github_pr_number": github_pr_number,
            "stage": stage_state_operation_view(stage) if stage is not None else None,
            "idempotent": existing_url == pr_url,
        }

    registry.register(
        name="record_pr_opened",
        description="Persist PR metadata without changing pr stage state.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "pr_url": {"type": "string"},
                "github_pr_number": {"type": ["integer", "null"]},
            },
            "required": ["task_id", "pr_url"],
        },
        output_schema={"type": "object"},
        func=record_pr_opened,
    )

    def record_merge_result(
        task_id: str,
        merge_sha: str | None = None,
        report_ref: str | None = None,
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        """Phase 2 registration stub for merge result recording."""
        raise NotImplementedError("wired in Phase 4.2")

    registry.register(
        name="record_merge_result",
        description="Persist merge outcome and advance/fail merge stage. Stubbed until Phase 4.2.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "merge_sha": {"type": ["string", "null"]},
                "report_ref": {"type": ["string", "null"]},
                "failure_reason": {"type": ["string", "null"]},
            },
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=record_merge_result,
    )

    return registry
