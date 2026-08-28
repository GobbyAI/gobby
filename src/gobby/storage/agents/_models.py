"""Models for agent run storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gobby.agents.resume_metadata import normalize_resume_metadata
from gobby.utils.datetime import normalize_datetime_model
from gobby.utils.machine_id import require_machine_id

from ._constants import AgentRunStatus, AgentRunTerminalReason


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "started_at",
        "completed_at",
    ),
)
@dataclass
class AgentRun:
    """Agent run data model."""

    id: str
    parent_session_id: str
    machine_id: str = field(default_factory=require_machine_id, kw_only=True)
    provider: str
    prompt: str
    status: AgentRunStatus
    created_at: datetime
    updated_at: datetime
    # Optional fields
    child_session_id: str | None = None
    claimed_session_id: str | None = None
    workflow_name: str | None = None
    agent_name: str | None = None
    model: str | None = None
    is_local: bool = False
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    reasoning_required: bool = False
    reasoning_status: str = "not_requested"
    reasoning_message: str | None = None
    result: str | None = None
    error: str | None = None
    tool_calls_count: int = 0
    turns_used: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    sdk_session_id: str | None = None
    continuation_prompt: str | None = None
    task_id: str | None = None
    pid: int | None = None
    terminal_id: str | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    timeout_seconds: float | None = None
    terminal_reason: AgentRunTerminalReason | None = None
    resume_metadata_json: dict[str, Any] | None = None
    capture_id: str | None = None
    capture_revision: int = 0
    pending_terminal_action: str | None = None
    pending_terminal_reason: str | None = None
    termination_requested_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentRun:
        """Create AgentRun from database row."""
        is_local: bool
        if "is_local" in row.keys() and row["is_local"] is not None:
            is_local = bool(row["is_local"])
        else:
            is_local = False

        return cls(
            id=row["id"],
            parent_session_id=row["parent_session_id"],
            machine_id=str(row["machine_id"]),
            child_session_id=row["child_session_id"],
            claimed_session_id=(
                row["claimed_session_id"] if "claimed_session_id" in row.keys() else None
            ),
            workflow_name=row["workflow_name"],
            agent_name=row["agent_name"] if "agent_name" in row.keys() else None,
            provider=row["provider"],
            model=row["model"],
            is_local=is_local,
            requested_reasoning_effort=(
                row["requested_reasoning_effort"]
                if "requested_reasoning_effort" in row.keys()
                else None
            ),
            effective_reasoning_effort=(
                row["effective_reasoning_effort"]
                if "effective_reasoning_effort" in row.keys()
                else None
            ),
            reasoning_required=bool(row["reasoning_required"])
            if "reasoning_required" in row.keys()
            else False,
            reasoning_status=(
                row["reasoning_status"] if "reasoning_status" in row.keys() else "not_requested"
            ),
            reasoning_message=(
                row["reasoning_message"] if "reasoning_message" in row.keys() else None
            ),
            status=row["status"],
            prompt=row["prompt"],
            result=row["result"],
            error=row["error"],
            tool_calls_count=row["tool_calls_count"] or 0,
            turns_used=row["turns_used"] or 0,
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sdk_session_id=row["sdk_session_id"] if "sdk_session_id" in row.keys() else None,
            continuation_prompt=row["continuation_prompt"]
            if "continuation_prompt" in row.keys()
            else None,
            task_id=row["task_id"] if "task_id" in row.keys() else None,
            pid=row["pid"] if "pid" in row.keys() else None,
            terminal_id=(
                str(row["terminal_id"])
                if "terminal_id" in row.keys() and row["terminal_id"] is not None
                else None
            ),
            worktree_id=row["worktree_id"] if "worktree_id" in row.keys() else None,
            clone_id=row["clone_id"] if "clone_id" in row.keys() else None,
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row.keys() else None,
            terminal_reason=row["terminal_reason"] if "terminal_reason" in row.keys() else None,
            resume_metadata_json=normalize_resume_metadata(
                row["resume_metadata_json"] if "resume_metadata_json" in row.keys() else None
            ),
            capture_id=row["capture_id"] if "capture_id" in row.keys() else None,
            capture_revision=(row["capture_revision"] or 0)
            if "capture_revision" in row.keys()
            else 0,
            pending_terminal_action=row["pending_terminal_action"]
            if "pending_terminal_action" in row.keys()
            else None,
            pending_terminal_reason=row["pending_terminal_reason"]
            if "pending_terminal_reason" in row.keys()
            else None,
            termination_requested_at=row["termination_requested_at"]
            if "termination_requested_at" in row.keys()
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        from gobby.storage.agents._sandbox_records import sandbox_record

        return {
            "run_id": self.id,
            "id": self.id,
            "session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "machine_id": self.machine_id,
            "child_session_id": self.child_session_id,
            "claimed_session_id": self.claimed_session_id,
            "workflow_name": self.workflow_name,
            "agent_name": self.agent_name,
            "provider": self.provider,
            "model": self.model,
            "is_local": self.is_local,
            "requested_reasoning_effort": self.requested_reasoning_effort,
            "effective_reasoning_effort": self.effective_reasoning_effort,
            "reasoning_required": self.reasoning_required,
            "reasoning_status": self.reasoning_status,
            "reasoning_message": self.reasoning_message,
            "status": self.status,
            "prompt": self.prompt,
            "result": self.result,
            "error": self.error,
            "tool_calls_count": self.tool_calls_count,
            "turns_used": self.turns_used,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "task_id": self.task_id,
            "pid": self.pid,
            "terminal_id": self.terminal_id,
            "worktree_id": self.worktree_id,
            "clone_id": self.clone_id,
            "timeout_seconds": self.timeout_seconds,
            "continuation_prompt": self.continuation_prompt,
            "terminal_reason": self.terminal_reason,
            "resume_metadata_json": self.resume_metadata_json,
            "capture_id": self.capture_id,
            "capture_revision": self.capture_revision,
            "pending_terminal_action": self.pending_terminal_action,
            "pending_terminal_reason": self.pending_terminal_reason,
            "termination_requested_at": self.termination_requested_at,
            "sandbox": sandbox_record(self.resume_metadata_json, include_events=True),
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        from gobby.storage.agents._sandbox_records import sandbox_record

        return {
            "run_id": self.id,
            "session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "machine_id": self.machine_id,
            "started_at": self.started_at,
            "pid": self.pid,
            "agent_name": self.agent_name,
            "workflow_name": self.workflow_name,
            "provider": self.provider,
            "model": self.model,
            "is_local": self.is_local,
            "task_id": self.task_id,
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "tool_calls_count": self.tool_calls_count,
            "turns_used": self.turns_used,
            "resume_metadata_json": self.resume_metadata_json,
            "sandbox": sandbox_record(self.resume_metadata_json, include_events=False),
        }
