"""Coordinate frozen feedback review, durable outcomes, and consumption."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.feedback.actions import FeedbackActions, ReviewTaskManagerProtocol
from gobby.feedback.agent import (
    FeedbackReviewerError,
    FeedbackReviewerProtocol,
    FeedbackReviewerResult,
    validate_feedback_findings,
)
from gobby.feedback.digest import render_digest
from gobby.feedback.report import combine_report
from gobby.feedback.storage import FeedbackReviewStore, FeedbackRow, write_review_report
from gobby.prompts.loader import PromptLoader
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.config.sessions import FeedbackReviewConfig

# Give the reviewer two hours, leaving the cron wrapper's separate grace period for
# completion delivery and deterministic task/report finalization.
DISTILL_TOTAL_DEADLINE_SECONDS = 2 * 60 * 60.0


class FeedbackReviewService:
    """Run the nightly (or on-demand) session-feedback review loop."""

    def __init__(
        self,
        db: HubDatabase,
        reviewer: FeedbackReviewerProtocol,
        config: FeedbackReviewConfig,
        task_manager: ReviewTaskManagerProtocol | None,
    ) -> None:
        self.db = db
        self.store = FeedbackReviewStore(db)
        self.reviewer = reviewer
        self.config = config
        self.actions = FeedbackActions(db, config, task_manager)

    async def run_review(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Review an immutable batch; consume only observations with recorded outcomes."""
        batch = self.store.freeze_batch(self.config.max_rows_per_run, dry_run=dry_run)
        if batch is None:
            return {"status": "no_rows", "run_id": None, "rows_considered": 0}
        run_id, rows = batch
        reviewer_agent_run_id: str | None = None
        findings: dict[str, Any] = {}
        actions: dict[str, Any] = {}
        attempts: list[dict[str, Any]] = []
        report_path: str | None = None

        def checkpoint(current: dict[str, Any]) -> None:
            nonlocal actions
            actions = current
            actions["review_attempts"] = attempts
            actions["reviewer_agent_run_id"] = reviewer_agent_run_id
            actions["report_path"] = report_path
            self.store.save_progress(run_id, findings, actions)

        try:
            review_result = await self._distill(run_id, rows, attempts)
            reviewer_agent_run_id = review_result.agent_run_id
            report_path = review_result.report_path
            findings = review_result.findings
            checkpoint(actions)
            actions = await self.actions.apply(
                findings, rows, dry_run=dry_run, checkpoint=checkpoint
            )
            checkpoint(actions)
            status = "partial" if actions.get("failed") else "completed"
            report_rows, report_findings, report_actions = await asyncio.to_thread(
                self.store.report_inputs, run_id, findings, actions
            )
            digest = render_digest(
                report_rows,
                report_findings,
                report_actions,
                dry_run=dry_run,
                resolve_task=self.actions._resolve_feedback_task,
            )
            if review_result.summary_md:
                digest = combine_report(review_result.summary_md, digest)
            if report_path:
                await asyncio.to_thread(write_review_report, report_path, digest)
            if not dry_run:
                failed_ids = {
                    value
                    for outcome in actions.get("failed", [])
                    for value in outcome["observation_ids"]
                }
                reviewed = {row.id for row in rows} - failed_ids
                actions["rows_marked_reviewed"] = self.store.mark_reviewed(sorted(reviewed), run_id)
            self.store.finalize_run(
                run_id,
                status=status,
                findings=findings,
                actions=actions,
                digest_md=digest,
                error="Some task actions failed; their observations remain unreviewed"
                if actions.get("failed")
                else None,
            )
        except BaseException as exc:
            if isinstance(exc, FeedbackReviewerError):
                reviewer_agent_run_id = exc.agent_run_id
            actions["reviewer_agent_run_id"] = reviewer_agent_run_id
            actions["review_attempts"] = attempts
            self.store.finalize_run(
                run_id,
                status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                findings=findings or None,
                actions=actions,
                error=str(exc) or type(exc).__name__,
            )
            raise
        return {
            "status": status,
            "run_id": run_id,
            "dry_run": dry_run,
            "rows_considered": len(rows),
            "tasks_filed": len(actions.get("filed", [])),
            "deduplicated": actions.get("deduplicated", 0),
            "reviewer_agent_run_id": reviewer_agent_run_id,
            "report_path": report_path,
        }

    async def _distill(
        self, run_id: str, rows: list[FeedbackRow], attempts: list[dict[str, Any]]
    ) -> FeedbackReviewerResult:
        loader = PromptLoader(db=self.db)
        prompt = loader.render(
            self.config.prompt_path,
            {
                "run_id": run_id,
                "max_tasks": self.config.max_tasks_per_run,
            },
        )
        from gobby.feedback.agent import FeedbackReviewerLaunchError

        for index in range(2):
            attempt: dict[str, Any] = {
                "phase": "review",
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }
            attempts.append(attempt)
            self.store.save_progress(run_id, {}, {"review_attempts": attempts})
            try:
                result = await self.reviewer.review(
                    prompt, run_id=run_id, timeout_seconds=DISTILL_TOTAL_DEADLINE_SECONDS
                )
                attempt.update(phase="validation", agent_run_id=result.agent_run_id)
                findings = validate_feedback_findings(
                    result.findings,
                    agent_run_id=result.agent_run_id,
                    observation_ids=[row.id for row in rows],
                )
                attempt["status"] = "completed"
                return FeedbackReviewerResult(
                    agent_run_id=result.agent_run_id,
                    findings=findings,
                    summary_md=result.summary_md,
                    report_path=result.report_path,
                )
            except BaseException as exc:
                attempt.update(
                    status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                    error=str(exc) or type(exc).__name__,
                )
                if isinstance(exc, FeedbackReviewerError):
                    attempt["agent_run_id"] = exc.agent_run_id
                if isinstance(exc, FeedbackReviewerLaunchError):
                    attempt["phase"] = "launch"
                    if index == 0 and exc.transient:
                        continue
                raise
            finally:
                attempt["completed_at"] = datetime.now(UTC).isoformat()
                current = self.store.get_run(run_id)
                saved_actions = current.actions or {} if current else {}
                saved_actions["review_attempts"] = attempts
                self.store.save_progress(
                    run_id, current.findings or {} if current else {}, saved_actions
                )
        raise RuntimeError("feedback review retry exhausted")
