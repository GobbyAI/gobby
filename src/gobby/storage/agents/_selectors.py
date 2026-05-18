"""Selector helpers for agent run storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from gobby.storage.database import DatabaseProtocol

from ._constants import ACTIVE_AGENT_RUN_STATUS_SQL
from ._models import AgentRun


class _AgentRunSelectorHost(Protocol):
    db: DatabaseProtocol

    def _select_runs_with_live_stats_sql(
        self,
        where_clause: str = "",
        order_by: str = "",
        *,
        limit: bool = False,
    ) -> str: ...


class _AgentRunSelectorMixin:
    @staticmethod
    def _select_runs_with_live_stats_sql(
        where_clause: str = "",
        order_by: str = "",
        *,
        limit: bool = False,
    ) -> str:
        """Build an agent-run SELECT that overlays live session stats for active runs."""
        sql = f"""
            SELECT
                ar.id,
                ar.parent_session_id,
                ar.child_session_id,
                ar.claimed_session_id,
                ar.workflow_name,
                ar.agent_name,
                ar.provider,
                ar.model,
                COALESCE(
                    ar.is_local,
                    CASE
                        WHEN lower(COALESCE(ar.provider, '')) IN (
                            'lmstudio', 'ollama', 'llamacpp', 'local'
                        )
                            OR lower(COALESCE(ar.model, '')) LIKE '%gpt-oss%'
                        THEN 1
                        ELSE 0
                    END
                ) AS is_local,
                ar.requested_reasoning_effort,
                ar.effective_reasoning_effort,
                ar.reasoning_required,
                ar.reasoning_status,
                ar.reasoning_message,
                ar.status,
                ar.prompt,
                ar.result,
                ar.error,
                CASE
                    WHEN ar.status IN ({ACTIVE_AGENT_RUN_STATUS_SQL}) THEN COALESCE(
                        child_s.tool_call_count,
                        CASE
                            WHEN child_s.id IS NULL THEN parent_s.tool_call_count
                        END,
                        ar.tool_calls_count,
                        0
                    )
                    ELSE COALESCE(ar.tool_calls_count, 0)
                END AS tool_calls_count,
                CASE
                    WHEN ar.status IN ({ACTIVE_AGENT_RUN_STATUS_SQL}) THEN COALESCE(
                        child_s.turn_count,
                        CASE
                            WHEN child_s.id IS NULL THEN parent_s.turn_count
                        END,
                        ar.turns_used,
                        0
                    )
                    ELSE COALESCE(ar.turns_used, 0)
                END AS turns_used,
                ar.started_at,
                ar.completed_at,
                ar.created_at,
                ar.updated_at,
                ar.sdk_session_id,
                ar.continuation_prompt,
                ar.task_id,
                ar.pid,
                ar.tmux_session_name,
                ar.worktree_id,
                ar.clone_id,
                ar.timeout_seconds,
                ar.terminal_reason
            FROM agent_runs ar
            LEFT JOIN sessions child_s ON child_s.id = ar.child_session_id
            LEFT JOIN sessions parent_s ON parent_s.id = ar.parent_session_id
            {where_clause}
            {order_by}
            """
        if limit:
            sql += "\n            LIMIT ?"
        return sql

    def _fetch_run_with_live_stats(
        self: _AgentRunSelectorHost,
        where_clause: str,
        params: Sequence[object],
    ) -> AgentRun | None:
        """Fetch one agent run through the live-stat selector."""
        row = self.db.fetchone(
            self._select_runs_with_live_stats_sql(where_clause),
            tuple(params),
        )
        return AgentRun.from_row(row) if row else None

    def _fetch_runs_with_live_stats(
        self: _AgentRunSelectorHost,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
    ) -> list[AgentRun]:
        """Fetch agent runs through the live-stat selector."""
        query_params = tuple(params)
        if limit is not None:
            query_params = (*query_params, limit)

        rows = self.db.fetchall(
            self._select_runs_with_live_stats_sql(
                where_clause,
                order_by,
                limit=limit is not None,
            ),
            query_params,
        )
        return [AgentRun.from_row(row) for row in rows]
