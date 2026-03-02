"""Session data model.

Contains the Session dataclass and its serialization helpers.
Extracted from src/gobby/storage/sessions.py as part of the
Strangler Fig decomposition.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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
    jsonl_path: str | None
    summary_path: str | None
    summary_markdown: str | None
    compact_markdown: str | None  # Handoff context for compaction
    git_branch: str | None
    parent_session_id: str | None
    created_at: str
    updated_at: str
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
    usage_total_cost_usd: float = 0.0
    context_window: int | None = None
    model: str | None = None  # LLM model used (e.g., "claude-3-5-sonnet-20241022")
    # Terminal context (JSON blob with tty, parent_pid, term_session_id, etc.)
    terminal_context: dict[str, Any] | None = None
    # Global sequence number
    seq_num: int | None = None
    # Edit history tracking
    had_edits: bool = False
    # Rolling conversation digest
    digest_markdown: str | None = None
    # Per-turn detailed record (overwritten each turn)
    last_turn_markdown: str | None = None
    # Persisted chat mode (plan, accept_edits, normal, bypass)
    chat_mode: str = "plan"
    # Idempotency guard for digest pipeline
    last_digest_input_hash: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Session:
        """Create Session from database row."""
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            machine_id=row["machine_id"],
            source=row["source"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            jsonl_path=row["jsonl_path"],
            summary_path=row["summary_path"],
            summary_markdown=row["summary_markdown"],
            compact_markdown=row["compact_markdown"],
            git_branch=row["git_branch"],
            parent_session_id=row["parent_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
            usage_total_cost_usd=row["usage_total_cost_usd"] or 0.0,
            context_window=row["context_window"] if "context_window" in row.keys() else None,
            model=row["model"] if "model" in row.keys() else None,
            terminal_context=cls._parse_terminal_context(row["terminal_context"]),
            seq_num=row["seq_num"] if "seq_num" in row.keys() else None,
            had_edits=bool(row["had_edits"]) if "had_edits" in row.keys() else False,
            digest_markdown=row["digest_markdown"] if "digest_markdown" in row.keys() else None,
            last_turn_markdown=row["last_turn_markdown"]
            if "last_turn_markdown" in row.keys()
            else None,
            chat_mode=row["chat_mode"] if "chat_mode" in row.keys() else "plan",
            last_digest_input_hash=row["last_digest_input_hash"]
            if "last_digest_input_hash" in row.keys()
            else None,
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
    def _parse_json_field(cls, row: Any, field_name: str) -> dict[str, Any] | None:
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
            logger.warning(f"Failed to parse {field_name} JSON, returning None")
            return None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "ref": f"#{self.seq_num}" if self.seq_num else self.id[:8],
            "external_id": self.external_id,
            "machine_id": self.machine_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "jsonl_path": self.jsonl_path,
            "summary_path": self.summary_path,
            "summary_markdown": self.summary_markdown,
            "compact_markdown": self.compact_markdown,
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
            "usage_total_cost_usd": self.usage_total_cost_usd,
            "context_window": self.context_window,
            "model": self.model,
            "terminal_context": self.terminal_context,
            "had_edits": self.had_edits,
            "digest_markdown": self.digest_markdown,
            "last_turn_markdown": self.last_turn_markdown,
            "chat_mode": self.chat_mode,
            "last_digest_input_hash": self.last_digest_input_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seq_num": self.seq_num,
            "id": self.id,  # UUID at end for backwards compat
        }
