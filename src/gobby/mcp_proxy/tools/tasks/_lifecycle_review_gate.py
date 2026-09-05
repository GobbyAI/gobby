"""One-shot and agentic task-close criteria review routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    ValidationResult,
    account_criteria_verdict,
)
from gobby.storage.tasks import Task
from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict
from gobby.tasks.criteria_contract import missing_operational_evidence, operational_evidence_hint
from gobby.tasks.validation import PreparedCloseReview, TaskValidator


@dataclass(frozen=True, slots=True)
class SubmittedCloseReview:
    """Authenticated validator verdict plus the intent fingerprints it must match."""

    verdict: Mapping[str, object]
    review_fingerprint: str
    evidence_fingerprint: str
    diff_sha: str | None = None
    test_bodies_sha: str | None = None
    stable_facts: Mapping[str, object] | None = None


_TEST_BODY_REFERENCE_RE = re.compile(r"^###\s+(?P<reference>.+?)\s*$", re.MULTILINE)


def _is_spawned_agent_caller(ctx: RegistryContext, session_id: str | None) -> bool:
    if session_id is None:
        return False
    row = ctx.task_manager.db.fetchone(
        "SELECT id FROM agent_runs WHERE child_session_id = %s LIMIT 1",
        (session_id,),
    )
    try:
        run_id = row["id"] if row is not None else None
    except (KeyError, TypeError, IndexError):
        return False
    return isinstance(run_id, str) and bool(run_id)


def _sequence_delta(label: str, before: object, after: object) -> str | None:
    if not isinstance(before, (list, tuple)) or not isinstance(after, (list, tuple)):
        return f"{label} changed" if before != after else None
    previous = [str(value) for value in before]
    current = [str(value) for value in after]
    added = [value for value in current if value not in previous]
    removed = [value for value in previous if value not in current]
    changes = [*(f"+{value}" for value in added), *(f"-{value}" for value in removed)]
    return f"{label} {', '.join(changes)}" if changes else None


def _fingerprint_deltas(
    prepared: PreparedCloseReview,
    submitted: SubmittedCloseReview,
    *,
    test_bodies: str,
) -> list[str]:
    current_diff_sha = prepared.diff_sha
    current_test_bodies_sha = prepared.test_bodies_sha
    current_facts = prepared.stable_facts
    deltas: list[str] = []

    if submitted.diff_sha != current_diff_sha:
        deltas.append("committed diff changed")
    if submitted.test_bodies_sha != current_test_bodies_sha:
        references = _TEST_BODY_REFERENCE_RE.findall(test_bodies)
        if references:
            deltas.extend(f"acceptance test body changed: {reference}" for reference in references)
        else:
            deltas.append("acceptance test bodies changed")

    if isinstance(current_facts, Mapping) and submitted.stable_facts is not None:
        previous_facts = submitted.stable_facts
        for key, label in (
            ("commit_shas", "commit set"),
            ("attributed_paths", "attributed paths"),
        ):
            delta = _sequence_delta(label, previous_facts.get(key), current_facts.get(key))
            if delta is not None:
                deltas.append(delta)
        if previous_facts.get("had_attributed_edits") != current_facts.get("had_attributed_edits"):
            deltas.append("attributed edit state changed")
        if previous_facts.get("commit_count") != current_facts.get(
            "commit_count"
        ) and previous_facts.get("commit_shas") == current_facts.get("commit_shas"):
            deltas.append("commit count changed")
    elif submitted.stable_facts != current_facts:
        deltas.append("stable checklist facts changed")

    return deltas


async def evaluate_close_criteria(
    *,
    task: Task,
    task_validator: TaskValidator,
    ctx: RegistryContext,
    resolved_id: str,
    changes_summary: str,
    diff_text: str,
    checklist_facts: Mapping[str, object],
    validation_config: TaskValidationConfig | None,
    reason: str,
    description: str,
    test_bodies: str,
    closing_session_id: str | None = None,
    submitted_review: SubmittedCloseReview | None = None,
) -> ValidationResult:
    """Detach a new review or account for an authenticated background verdict."""
    spawned_agent_caller = _is_spawned_agent_caller(ctx, closing_session_id)
    raw_transcript_actions = checklist_facts.get("transcript_operational_actions", ())
    transcript_actions = (
        tuple(str(action) for action in raw_transcript_actions)
        if isinstance(raw_transcript_actions, (list, tuple, set, frozenset))
        else ()
    )
    missing_operations = (
        missing_operational_evidence(
            task.validation_criteria,
            changes_summary,
            transcript_actions=transcript_actions,
            skip_external=spawned_agent_caller,
        )
        if reason == "completed"
        else ()
    )
    if missing_operations:
        missing_text = ", ".join(missing_operations)
        return ValidationResult(
            can_close=False,
            error_type="operational_evidence_missing",
            message=(
                "Task-close evidence does not confirm required operational actions: "
                f"{missing_text}. Record each completed action and outcome in changes_summary "
                "or run a matching successful command in the task transcript."
            ),
            extra={
                "blocking_reasons": [
                    f"Missing completion evidence for operational actions: {missing_text}."
                ],
                "missing_operational_actions": list(missing_operations),
                "operational_evidence_hint": operational_evidence_hint(
                    task.validation_criteria,
                    missing_operations,
                ),
            },
        )
    prepared = task_validator.prepare_task_review(
        title=task.title,
        changes_summary=changes_summary,
        validation_criteria=task.validation_criteria or "",
        diff_text=diff_text,
        checklist_facts=checklist_facts,
        closure_reason=reason,
        description=description,
        test_bodies=test_bodies,
    )
    if submitted_review is not None:
        invalidating_deltas = _fingerprint_deltas(
            prepared,
            submitted_review,
            test_bodies=test_bodies,
        )
        fingerprints_changed = (
            prepared.review_fingerprint != submitted_review.review_fingerprint
            or prepared.evidence_fingerprint != submitted_review.evidence_fingerprint
        )
        if fingerprints_changed or invalidating_deltas:
            if not invalidating_deltas:
                invalidating_deltas.append("review prompt changed")
            invalidating_delta = "; ".join(invalidating_deltas)
            return ValidationResult(
                can_close=False,
                error_type="agentic_review_stale",
                message=(
                    "Task-close evidence changed after the background review launched. "
                    f"Invalidating delta: {invalidating_delta}."
                ),
                extra={
                    "stale_state": True,
                    "invalidating_delta": invalidating_delta,
                    "invalidating_deltas": invalidating_deltas,
                    "review_fingerprint": prepared.review_fingerprint,
                    "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
                },
            )
        try:
            verdict = parse_close_verdict(
                submitted_review.verdict,
                list(prepared.criteria),
                defer_external_criteria=spawned_agent_caller,
            )
        except CloseVerdictParseError as exc:
            return ValidationResult(
                can_close=False,
                error_type="agentic_review_malformed",
                message=f"Background close-review verdict is invalid: {exc}",
            )
        accounted = account_criteria_verdict(
            task=task,
            verdict=verdict,
            ctx=ctx,
            resolved_id=resolved_id,
            validation_config=validation_config,
            reset_reason="agentic_valid",
        )
        accounted.extra.update(
            {
                "review_fingerprint": prepared.review_fingerprint,
                "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
            }
        )
        return accounted

    return ValidationResult(
        can_close=False,
        error_type="agentic_review_required",
        message=(
            "Complete close evidence requires a daemon-managed task-close validator; "
            "its verdict is applied and delivered automatically."
        ),
        extra={
            "prompt_chars": prepared.prompt_chars,
            "prompt_limit": prepared.prompt_limit,
            "review_fingerprint": prepared.review_fingerprint,
            "deterministic_evidence_fingerprint": prepared.evidence_fingerprint,
            "diff_sha": prepared.diff_sha,
            "test_bodies_sha": prepared.test_bodies_sha,
            "stable_facts": prepared.stable_facts,
            "manifest_count": prepared.manifest_count,
            "excerpt_chars": prepared.excerpt_chars,
            "coordinator_owned_pending": spawned_agent_caller,
            # Gate 10's run record travels to the validator launch prompt; the
            # taskless validator cannot read the transcript itself.
            "validation_commands": checklist_facts.get("validation_commands"),
        },
    )


__all__ = ["SubmittedCloseReview", "evaluate_close_criteria"]
