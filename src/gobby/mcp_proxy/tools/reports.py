"""MCP adapters for evidence-driven synthesis publication."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.feedback.storage import FeedbackReviewStore
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.reports.storage import ReportStore, report_path
from gobby.storage.hub.protocol import HubDatabase


def create_reports_registry(db: HubDatabase, *, project_id: str | None) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-reports", description="Synthesis report evidence and publication"
    )
    store = ReportStore(db)

    @registry.tool(
        name="request_report",
        description="Queue a report for a terminal source run without replaying any source operations.",
    )
    async def request_report(source_kind: str, run_id: str) -> dict[str, Any]:
        if project_id is None:
            raise ValueError("Report publication requires a repository project")
        return await asyncio.to_thread(store.request, source_kind, run_id, project_id)

    @registry.tool(
        name="get_report",
        description="Read publication status, task, branch, commit references and any persisted report draft.",
    )
    async def get_report(source_kind: str, run_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(store.get, source_kind, run_id)

    @registry.tool(
        name="get_report_attempts",
        description="Read a page of publication attempts, including failed attempts and diagnostics.",
    )
    async def get_report_attempts(
        source_kind: str, run_id: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            store.attempts, source_kind, run_id, offset=offset, limit=limit
        )

    @registry.tool(
        name="get_report_evidence",
        description="Read source outcomes with pagination; historical Dream evidence includes authoritative snapshots and explicitly missing rationale.",
    )
    async def get_report_evidence(
        source_kind: str, run_id: str, offset: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        report_path(source_kind, run_id)
        if source_kind == "feedback":
            return await asyncio.to_thread(
                FeedbackReviewStore(db).results_page, run_id, offset=offset, limit=limit
            )
        evidence = await asyncio.to_thread(
            DreamDecisionStore(db).page, run_id, offset=offset, limit=limit
        )
        source = await asyncio.to_thread(MemoryDreamStore(db).get_run, run_id)
        if source is not None:
            evidence["source_status"] = source["status"]
            evidence["source_error"] = source.get("error")
            evidence["summary"] = source.get("summary")
            evidence["child_runs"] = (source.get("plan") or {}).get("runs", [])
        if evidence["historical_rationale_missing"]:
            snapshots = await asyncio.to_thread(
                db.fetchall,
                "SELECT * FROM memory_dream_snapshots WHERE run_id = %s ORDER BY id LIMIT %s OFFSET %s",
                (run_id, limit + 1, offset),
            )
            evidence["snapshots"] = [dict(row) for row in snapshots[:limit]]
            evidence["next_offset"] = offset + limit if len(snapshots) > limit else None
            evidence["historical_rationale_missing"] = True
        return evidence

    @registry.tool(
        name="save_report_draft",
        description="Validate and durably save Markdown before Git publication; use the exact returned path and bytes.",
    )
    async def save_report_draft(source_kind: str, run_id: str, content: str) -> dict[str, Any]:
        return await asyncio.to_thread(store.save_draft, source_kind, run_id, content)

    @registry.tool(
        name="record_report_failure",
        description="Preserve an exact reporting failure and phase; saved report content remains available for publication recovery.",
    )
    async def record_report_failure(
        source_kind: str, run_id: str, phase: str, error: str
    ) -> dict[str, Any]:
        await asyncio.to_thread(store.record_failure, source_kind, run_id, phase, error)
        return await asyncio.to_thread(store.get, source_kind, run_id, include_content=False)

    @registry.tool(
        name="retry_report",
        description="Retry failed or interrupted report publication using its existing task and saved draft; source operations are never rerun.",
    )
    async def retry_report(source_kind: str, run_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(store.retry, source_kind, run_id)

    return registry
