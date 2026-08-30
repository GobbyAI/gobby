"""Session data model.

Contains the Session dataclass and its serialization helpers.
Extracted from src/gobby/storage/sessions.py as part of the
Strangler Fig decomposition.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
from gobby.utils.datetime import normalize_datetime_model

logger = logging.getLogger(__name__)


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "last_activity",
        "summary_generated_at",
        "context_usage_updated_at",
    ),
)
@dataclass
class Session:
    """Session data model."""

    id: str
    external_id: str
    machine_id: str
    source: str
    project_id: str  # Required - sessions must belong to a project
    title: str | None
    status: str
    transcript_path: str | None
    summary_path: str | None
    summary_markdown: str | None
    git_branch: str | None
    parent_session_id: str | None
    created_at: datetime
    updated_at: datetime
    # Bumped only by confirmed agent/user activity, never by lifecycle status
    # writes — the trustworthy idle-decision timestamp (updated_at is not).
    last_activity: datetime | None = None
    handoff_markdown: str | None = None
    summary_revision_id: str | None = None
    summary_source_context_hash: str | None = None
    summary_generation_mode: str | None = None
    summary_generated_at: datetime | None = None
    title_source: str | None = None
    agent_depth: int = 0  # 0 = human-initiated, 1+ = agent-spawned
    spawned_by_agent_id: str | None = None  # ID of agent that spawned this session
    # Terminal pickup metadata fields
    workflow_name: str | None = None  # Workflow to activate on terminal pickup
    agent_run_id: str | None = None  # Link back to agent run record
    context_injected: bool = False  # Whether context was injected into prompt
    original_prompt: str | None = None  # Original prompt for terminal mode
    # Usage tracking fields
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_cache_creation_tokens: int = 0
    usage_cache_read_tokens: int = 0
    context_window: int | None = None
    context_used_tokens: int | None = None
    context_usage_ratio: float | None = None
    context_usage_source: str | None = None
    context_usage_confidence: str | None = None
    context_usage_updated_at: datetime | None = None
    last_prompt_input_tokens: int | None = None
    last_prompt_uncached_input_tokens: int | None = None
    last_prompt_cache_read_tokens: int | None = None
    last_prompt_cache_creation_tokens: int | None = None
    last_completion_output_tokens: int | None = None
    model: str | None = None  # LLM model used (e.g., "claude-3-5-sonnet-20241022")
    is_local: bool = False
    # Terminal context (JSON blob with tty, parent_pid, tmux_pane, term_program)
    terminal_context: dict[str, Any] | None = None
    # Global sequence number
    seq_num: int | None = None
    # Edit history tracking
    had_edits: bool = False
    # Persisted chat mode (plan, accept_edits, normal, bypass)
    chat_mode: str = "plan"
    # Stats fields
    message_count: int = 0
    turn_count: int = 0
    tool_call_count: int = 0
    last_assistant_content: str | None = None
    # JSON array of user-approved tool names (approve_always)
    approved_tools_json: str | None = None
    # Session type: 'terminal' (CLI) or 'web_chat' (browser UI)
    session_type: str = "terminal"
    sandbox_enabled: bool | None = False
    sandbox_policy_hash: str | None = None
    workspace_path: str | None = None
    workspace_generation: int = 0
    # Task-ref enrichment populated post-load by callers that join the tasks
    # table. Default empty so unenriched Session instances serialize cleanly.
    claimed_task_refs: list[int] = field(default_factory=list)
    created_task_refs: list[int] = field(default_factory=list)
    closed_task_refs: list[int] = field(default_factory=list)

    @staticmethod
    def _get_optional(row: Mapping[str, Any], key: str) -> Any | None:
        return row[key] if key in row.keys() else None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Session:
        """Create Session from database row."""
        is_local: bool
        if "is_local" in row.keys() and row["is_local"] is not None:
            is_local = bool(row["is_local"])
        else:
            is_local = False

        return cls(
            id=row["id"],
            external_id=row["external_id"],
            machine_id=row["machine_id"],
            source=row["source"],
            project_id=row["project_id"],
            title=row["title"],
            title_source=cls._get_optional(row, "title_source"),
            status=row["status"],
            transcript_path=row["transcript_path"],
            summary_path=row["summary_path"],
            summary_markdown=row["summary_markdown"],
            handoff_markdown=cls._get_optional(row, "handoff_markdown"),
            git_branch=row["git_branch"],
            parent_session_id=row["parent_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_activity=cls._get_optional(row, "last_activity"),
            summary_revision_id=cls._get_optional(row, "summary_revision_id"),
            summary_source_context_hash=cls._get_optional(row, "summary_source_context_hash"),
            summary_generation_mode=cls._get_optional(row, "summary_generation_mode"),
            summary_generated_at=cls._get_optional(row, "summary_generated_at"),
            agent_depth=row["agent_depth"] or 0,
            spawned_by_agent_id=row["spawned_by_agent_id"],
            workflow_name=row["workflow_name"],
            agent_run_id=row["agent_run_id"],
            context_injected=bool(row["context_injected"]),
            original_prompt=row["original_prompt"],
            usage_input_tokens=row["usage_input_tokens"] or 0,
            usage_output_tokens=row["usage_output_tokens"] or 0,
            usage_cache_creation_tokens=row["usage_cache_creation_tokens"] or 0,
            usage_cache_read_tokens=row["usage_cache_read_tokens"] or 0,
            context_window=cls._get_optional(row, "context_window"),
            context_used_tokens=cls._get_optional(row, "context_used_tokens"),
            context_usage_ratio=cls._get_optional(row, "context_usage_ratio"),
            context_usage_source=cls._get_optional(row, "context_usage_source"),
            context_usage_confidence=cls._get_optional(row, "context_usage_confidence"),
            context_usage_updated_at=cls._get_optional(row, "context_usage_updated_at"),
            last_prompt_input_tokens=cls._get_optional(row, "last_prompt_input_tokens"),
            last_prompt_uncached_input_tokens=cls._get_optional(
                row, "last_prompt_uncached_input_tokens"
            ),
            last_prompt_cache_read_tokens=cls._get_optional(row, "last_prompt_cache_read_tokens"),
            last_prompt_cache_creation_tokens=cls._get_optional(
                row, "last_prompt_cache_creation_tokens"
            ),
            last_completion_output_tokens=cls._get_optional(row, "last_completion_output_tokens"),
            model=cls._get_optional(row, "model"),
            is_local=is_local,
            terminal_context=cls._parse_terminal_context(row["terminal_context"]),
            seq_num=row["seq_num"] if "seq_num" in row.keys() else None,
            had_edits=bool(row["had_edits"]) if "had_edits" in row.keys() else False,
            chat_mode=row["chat_mode"] if "chat_mode" in row.keys() else "plan",
            message_count=row["message_count"] if "message_count" in row.keys() else 0,
            turn_count=row["turn_count"] if "turn_count" in row.keys() else 0,
            tool_call_count=row["tool_call_count"] if "tool_call_count" in row.keys() else 0,
            last_assistant_content=row["last_assistant_content"]
            if "last_assistant_content" in row.keys()
            else None,
            approved_tools_json=row["approved_tools_json"]
            if "approved_tools_json" in row.keys()
            else None,
            session_type=row["session_type"] if "session_type" in row.keys() else "terminal",
            sandbox_enabled=(
                bool(row["sandbox_enabled"]) if row["sandbox_enabled"] is not None else None
            )
            if "sandbox_enabled" in row.keys()
            else False,
            sandbox_policy_hash=row["sandbox_policy_hash"]
            if "sandbox_policy_hash" in row.keys()
            else None,
            workspace_path=cls._get_optional(row, "workspace_path"),
            workspace_generation=int(row["workspace_generation"] or 0)
            if "workspace_generation" in row.keys()
            else 0,
        )

    @classmethod
    def _parse_terminal_context(cls, raw: str | None) -> dict[str, Any] | None:
        """Parse terminal_context JSON, returning None on malformed data.

        Args:
            raw: Raw JSON string or None

        Returns:
            Parsed dict or None if parsing fails or input is None
        """
        if not raw:
            return None
        try:
            result: dict[str, Any] = json.loads(raw)
            return result
        except json.JSONDecodeError:
            logger.warning("Failed to parse terminal_context JSON, returning None")
            return None

    @classmethod
    def _parse_json_field(cls, row: Mapping[str, Any], field_name: str) -> dict[str, Any] | None:
        """Parse a JSON field from a database row, returning None on missing/malformed data."""
        if field_name not in row.keys():
            return None
        raw = row[field_name]
        if not raw:
            return None
        try:
            result: dict[str, Any] = json.loads(raw)
            return result
        except json.JSONDecodeError:
            logger.warning("Failed to parse %s JSON, returning None", field_name)
            return None

    @property
    def ref(self) -> str:
        """Short human-readable reference: #seq_num or first 8 chars of id."""
        return f"#{self.seq_num}" if self.seq_num else self.id[:8]

    @property
    def has_terminal_liveness(self) -> bool:
        """Best-effort durable liveness signal for tmux-backed terminal sessions."""
        if self.status not in TERMINAL_OWNER_STATUSES:
            return False
        if not self.terminal_context:
            return False

        tmux_pane = self.terminal_context.get("tmux_pane")
        if isinstance(tmux_pane, str) and tmux_pane:
            return True

        return False

    @property
    def can_proxy_attach(self) -> bool:
        """Whether the web chat can proxy-attach to this session right now."""
        if self.session_type != "terminal":
            return False
        return self.has_terminal_liveness

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "ref": self.ref,
            "external_id": self.external_id,
            "machine_id": self.machine_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "title_source": self.title_source,
            "status": self.status,
            "transcript_path": self.transcript_path,
            "summary_path": self.summary_path,
            "summary_markdown": self.summary_markdown,
            "handoff_markdown": self.handoff_markdown,
            "summary_revision_id": self.summary_revision_id,
            "summary_source_context_hash": self.summary_source_context_hash,
            "summary_generation_mode": self.summary_generation_mode,
            "summary_generated_at": self.summary_generated_at,
            "git_branch": self.git_branch,
            "parent_session_id": self.parent_session_id,
            "agent_depth": self.agent_depth,
            "spawned_by_agent_id": self.spawned_by_agent_id,
            "workflow_name": self.workflow_name,
            "agent_run_id": self.agent_run_id,
            "context_injected": self.context_injected,
            "original_prompt": self.original_prompt,
            "usage_input_tokens": self.usage_input_tokens,
            "usage_output_tokens": self.usage_output_tokens,
            "usage_cache_creation_tokens": self.usage_cache_creation_tokens,
            "usage_cache_read_tokens": self.usage_cache_read_tokens,
            "context_window": self.context_window,
            "context_used_tokens": self.context_used_tokens,
            "context_usage_ratio": self.context_usage_ratio,
            "context_usage_source": self.context_usage_source,
            "context_usage_confidence": self.context_usage_confidence,
            "context_usage_updated_at": self.context_usage_updated_at,
            "last_prompt_input_tokens": self.last_prompt_input_tokens,
            "last_prompt_uncached_input_tokens": self.last_prompt_uncached_input_tokens,
            "last_prompt_cache_read_tokens": self.last_prompt_cache_read_tokens,
            "last_prompt_cache_creation_tokens": self.last_prompt_cache_creation_tokens,
            "last_completion_output_tokens": self.last_completion_output_tokens,
            "model": self.model,
            "is_local": self.is_local,
            "terminal_context": self.terminal_context,
            "had_edits": self.had_edits,
            "chat_mode": self.chat_mode,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
            "last_assistant_content": self.last_assistant_content,
            "approved_tools_json": self.approved_tools_json,
            "session_type": self.session_type,
            "sandbox_enabled": self.sandbox_enabled,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "workspace_path": self.workspace_path,
            "workspace_generation": self.workspace_generation,
            "can_proxy_attach": self.can_proxy_attach,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity": self.last_activity,
            "seq_num": self.seq_num,
            "claimed_task_refs": self.claimed_task_refs,
            "created_task_refs": self.created_task_refs,
            "closed_task_refs": self.closed_task_refs,
            "id": self.id,  # UUID at end for backwards compat
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        return {
            "ref": self.ref,
            "external_id": self.external_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "title_source": self.title_source,
            "status": self.status,
            "git_branch": self.git_branch,
            "summary_revision_id": self.summary_revision_id,
            "summary_generation_mode": self.summary_generation_mode,
            "summary_generated_at": self.summary_generated_at,
            "model": self.model,
            "is_local": self.is_local,
            "had_edits": self.had_edits,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
            "session_type": self.session_type,
            "sandbox_enabled": self.sandbox_enabled,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "workspace_path": self.workspace_path,
            "workspace_generation": self.workspace_generation,
            "can_proxy_attach": self.can_proxy_attach,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity": self.last_activity,
            "seq_num": self.seq_num,
            "claimed_task_refs": self.claimed_task_refs,
            "created_task_refs": self.created_task_refs,
            "closed_task_refs": self.closed_task_refs,
            "id": self.id,
        }
