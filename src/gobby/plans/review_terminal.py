"""One evidence-aware terminal transition for plan-review agent runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol, cast

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    validate_round_result,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_telemetry import (
    derive_daemon_aggregates,
    enrich_round_result,
    persist_enriched_round_result,
    validate_convergence_telemetry,
)
from gobby.plans.review_verdict_effects import apply_staged_verdict_effects
from gobby.storage.agents import AgentRun, AgentRunTerminalReason
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._transitions import reset_current_non_ready_stage
from gobby.utils.datetime import utc_now

TerminalAction = Literal["complete", "fail", "timeout", "cancel"]


class AgentRunTerminalStorage(Protocol):
    db: HubDatabase

    def get(self, run_id: str) -> AgentRun | None: ...

    def complete(
        self,
        run_id: str,
        result: str | None = None,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> AgentRun | None: ...

    def fail(
        self,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
        result: str | None = None,
    ) -> AgentRun | None: ...

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
        result: str | None = None,
    ) -> AgentRun | None: ...

    def cancel(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason | None = None,
        result: str | None = None,
    ) -> AgentRun | None: ...


@dataclass(frozen=True)
class PlanReviewTerminalOutcome:
    """Result of routing one terminal decision through the evidence guard."""

    handled: bool
    run: AgentRun | None = None
    evidence_id: str | None = None
    parent_session_id: str | None = None
    result: dict[str, object] | None = None
    expired: bool = False


def terminalize_plan_review_run(
    storage: AgentRunTerminalStorage,
    *,
    db: HubDatabase | None = None,
    run_id: str,
    action: TerminalAction,
    error: str | None = None,
    terminal_reason: AgentRunTerminalReason | None = None,
    timeout_seconds: float | None = None,
    tool_calls_count: int | None = None,
    turns_used: int | None = None,
    completed_at: datetime | None = None,
) -> PlanReviewTerminalOutcome:
    """Settle, enrich, and terminalize a bound review before any parent wake."""
    database = db or getattr(storage, "db", None)
    if database is None or not hasattr(database, "execute") or not hasattr(database, "fetchone"):
        return PlanReviewTerminalOutcome(handled=False)
    cursor = database.execute(
        "SELECT evidence_id FROM plan_review_evidence WHERE dispatch_run_id = %s",
        (run_id,),
    )
    if not hasattr(cursor, "fetchone"):
        return PlanReviewTerminalOutcome(handled=False)
    bound_row = cursor.fetchone()
    if not isinstance(bound_row, Mapping) or "evidence_id" not in bound_row:
        return PlanReviewTerminalOutcome(handled=False)
    evidence = PlanReviewEvidenceStore(database).get_by_dispatch_run(run_id)
    if evidence is None:
        return PlanReviewTerminalOutcome(handled=False)
    run = storage.get(run_id)
    if run is None:
        return PlanReviewTerminalOutcome(
            handled=True,
            evidence_id=evidence.evidence_id,
        )
    if run.status in {"success", "error", "timeout", "cancelled"}:
        return PlanReviewTerminalOutcome(
            handled=True,
            run=run,
            evidence_id=evidence.evidence_id,
            parent_session_id=run.parent_session_id,
            result=_parse_result(run.result),
            expired=evidence.expired_at is not None,
        )

    settled_at = completed_at or utc_now()
    final_tool_calls = run.tool_calls_count if tool_calls_count is None else tool_calls_count
    final_turns = run.turns_used if turns_used is None else turns_used
    aggregate_run = replace(
        run,
        tool_calls_count=final_tool_calls,
        turns_used=final_turns,
        completed_at=settled_at,
    )
    delivered = _delivered_result(run.result)
    expired = False

    if delivered is None:
        if action == "timeout":
            result = _timeout_result(
                evidence_id=evidence.evidence_id,
                run=aggregate_run,
                timeout_seconds=timeout_seconds or run.timeout_seconds or 1,
            )
            updated = storage.timeout(
                run_id,
                error=error or "Execution timed out",
                tool_calls_count=final_tool_calls,
                turns_used=final_turns,
                result=_encode_result(result),
            )
        else:
            result = None
            updated = storage.fail(
                run_id,
                error=error or "Bound plan-review evidence has no delivered result",
                tool_calls_count=final_tool_calls,
                turns_used=final_turns,
            )
        if updated is not None:
            PlanReviewEvidenceService(database).expire_plan_review_evidence(evidence.evidence_id)
            expired = True
            _restore_staged_review(database, evidence.task_id)
        return PlanReviewTerminalOutcome(
            handled=True,
            run=updated or storage.get(run_id),
            evidence_id=evidence.evidence_id,
            parent_session_id=run.parent_session_id,
            result=result,
            expired=expired,
        )

    telemetry = delivered["convergence_telemetry"]
    if not isinstance(telemetry, dict):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            "convergence_telemetry must be an object",
        )
    if telemetry.get("state") == "enriched":
        result = delivered
    else:
        result = enrich_round_result(
            delivered,
            run=aggregate_run,
            terminal_status=_terminal_status(action),
            completed_at=settled_at,
        )
        persist_enriched_round_result(
            database,
            run_id=run_id,
            round_result=result,
        )
    if evidence.task_id is not None:
        _commit_staged_verdict(
            database,
            evidence=evidence,
            run=run,
            result=result,
        )
        effect_evidence = PlanReviewEvidenceService(database).get_evidence(evidence.evidence_id)
        apply_staged_verdict_effects(
            database,
            evidence=effect_evidence,
            run=run,
            result=result,
        )
    encoded = _encode_result(result)
    if action == "complete":
        updated = storage.complete(
            run_id,
            result=encoded,
            tool_calls_count=final_tool_calls,
            turns_used=final_turns,
        )
    elif action == "timeout":
        updated = storage.timeout(
            run_id,
            error=error or "Execution timed out",
            tool_calls_count=final_tool_calls,
            turns_used=final_turns,
            result=encoded,
        )
    elif action == "cancel":
        updated = storage.cancel(
            run_id,
            terminal_reason=terminal_reason,
            result=encoded,
        )
    else:
        updated = storage.fail(
            run_id,
            error=error or "Agent run failed",
            tool_calls_count=final_tool_calls,
            turns_used=final_turns,
            result=encoded,
        )

    verdict = result.get("verdict")
    retryable_result = verdict == "inconclusive"
    if retryable_result and updated is not None:
        PlanReviewEvidenceService(database).expire_plan_review_evidence(evidence.evidence_id)
        expired = True
        _restore_staged_review(database, evidence.task_id)
    return PlanReviewTerminalOutcome(
        handled=True,
        run=updated or storage.get(run_id),
        evidence_id=evidence.evidence_id,
        parent_session_id=run.parent_session_id,
        result=result,
        expired=expired,
    )


def _delivered_result(raw: str | None) -> dict[str, object] | None:
    result = _parse_result(raw)
    if result is None:
        return None
    try:
        payload = validate_round_result(result)
        telemetry = payload.get("convergence_telemetry")
        if not isinstance(telemetry, dict):
            return None
        state = telemetry.get("state")
        if state == "delivered":
            validate_convergence_telemetry(telemetry, required_state="delivered")
        elif state == "enriched":
            validate_convergence_telemetry(telemetry, required_state="enriched")
        else:
            return None
        return payload
    except ReviewEvidenceError:
        return None


def _timeout_result(
    *,
    evidence_id: str,
    run: AgentRun,
    timeout_seconds: float,
) -> dict[str, object]:
    telemetry = {
        "state": "enriched",
        "reviewer": {
            "status": "unavailable",
            "reason": "reviewer_result_not_delivered",
        },
        "daemon": derive_daemon_aggregates(
            run,
            terminal_status="timeout",
            finding_count=0,
            completed_at=run.completed_at,
        ),
    }
    result = {
        "verdict": "inconclusive",
        "evidence_id": evidence_id,
        "reason": {
            "reason_code": "timeout",
            "timeout_seconds": timeout_seconds,
        },
        "convergence_telemetry": telemetry,
    }
    return validate_round_result(result)


def _terminal_status(action: TerminalAction) -> str:
    return {
        "complete": "success",
        "fail": "error",
        "timeout": "timeout",
        "cancel": "cancelled",
    }[action]


def _restore_staged_review(db: HubDatabase, task_id: str | None) -> None:
    if task_id is None:
        return
    reset_current_non_ready_stage(
        db,
        task_id,
        reason="plan_review_terminal_retry",
        by_actor="plan_review_terminal",
    )


def _commit_staged_verdict(
    db: HubDatabase,
    *,
    evidence: PlanReviewEvidence,
    run: AgentRun,
    result: dict[str, object],
) -> None:
    """Finalize evidence and commit a staged verdict after telemetry enrichment."""
    from gobby.storage.tasks._review_transitions import approve_review, reject_review

    telemetry = result["convergence_telemetry"]
    if not isinstance(telemetry, dict):
        raise ReviewEvidenceError(
            "invalid_staged_round_result",
            "staged round result requires telemetry",
        )
    task_id = evidence.task_id
    if task_id is None or result.get("verdict") == "inconclusive":
        return
    findings = result.get("findings")
    coverage_attestation = result.get("coverage_attestation")
    if not isinstance(findings, list) or not isinstance(coverage_attestation, dict):
        raise ReviewEvidenceError(
            "invalid_staged_round_result",
            "staged round result requires telemetry, findings, and coverage_attestation",
        )
    if result.get("verdict") == "approved":
        manifest_entries = result.get("manifest_entries")
        routing_decisions = result.get("routing_decisions")
        if not isinstance(manifest_entries, list) or not isinstance(routing_decisions, dict):
            raise ReviewEvidenceError(
                "invalid_staged_round_result",
                "approved staged result requires manifest_entries and routing_decisions",
            )
        approve_review(
            db,
            task_id,
            evidence.stage,
            round_number=evidence.round_number,
            findings=findings,
            manifest_entries=manifest_entries,
            routing_decisions=routing_decisions,
            coverage_attestation=coverage_attestation,
            convergence_telemetry=telemetry,
            evidence_id=evidence.evidence_id,
            by_session_id=run.child_session_id,
            dispatch_run_id=run.id,
        )
        return
    if result.get("verdict") == "needs_review":
        reject_review(
            db,
            task_id,
            evidence.stage,
            round_number=evidence.round_number,
            findings=findings,
            coverage_attestation=coverage_attestation,
            convergence_telemetry=telemetry,
            evidence_id=evidence.evidence_id,
            by_session_id=run.child_session_id,
            dispatch_run_id=run.id,
        )


def _parse_result(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _encode_result(result: dict[str, object]) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
