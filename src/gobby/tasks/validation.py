"""Structured evidence preparation for the task-close reviewer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from gobby.config.tasks import TaskValidationConfig
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.validation_evidence import build_close_diff_evidence

# Closure reasons that require no repository change: the criteria review judges
# the disposition justification instead of literal criterion satisfaction.
NO_WORK_CLOSE_REASONS: frozenset[str] = frozenset(
    {"duplicate", "already_implemented", "wont_fix", "obsolete", "out_of_repo"}
)

# Checklist facts that identify the deliverable a verdict was rendered against.
# Only these key the review and evidence fingerprints (#21675).
#
# The remaining facts — `validation_commands`, `transcript_operational_actions`,
# `acceptance_artifacts` and `tdd_evidence` — are derived from the live session
# transcript, so they change whenever the launching session runs another
# command. Keying on them made a session void its own in-flight verdict simply
# by continuing to work, and defeated the verdict memo at the same time, since
# an otherwise unchanged retry never presented the same key twice. Transcript
# evidence only ever grows, and evidence that grew cannot falsify a verdict that
# has already been rendered; a new commit or a new attributed edit still moves
# `commit_shas` and `attributed_paths` and correctly invalidates it.
STABLE_CHECKLIST_FACT_KEYS: frozenset[str] = frozenset(
    {
        "commit_count",
        "commit_shas",
        "had_attributed_edits",
        "attributed_paths",
    }
)


def stable_checklist_facts(checklist_facts: Mapping[str, object]) -> dict[str, object]:
    """Narrow checklist facts to the deliverable-identifying subset."""
    return {
        key: value for key, value in checklist_facts.items() if key in STABLE_CHECKLIST_FACT_KEYS
    }


@dataclass(frozen=True, slots=True)
class PreparedCloseReview:
    """Structured close-review evidence with stable submission fingerprints."""

    criteria: tuple[str, ...]
    review_fingerprint: str
    evidence_fingerprint: str
    diff_sha: str
    test_bodies_sha: str
    stable_facts: dict[str, object]
    manifest_count: int
    excerpt_chars: int


class TaskValidator:
    """Prepare immutable evidence for the daemon-managed close reviewer."""

    def __init__(self, config: TaskValidationConfig) -> None:
        self.config = config

    def prepare_task_review(
        self,
        *,
        title: str,
        changes_summary: str,
        validation_criteria: str,
        diff_text: str | None,
        checklist_facts: Mapping[str, object],
        closure_reason: str = "completed",
        description: str = "",
        test_bodies: str = "Named acceptance tests: none.",
    ) -> PreparedCloseReview:
        """Fingerprint the task and evidence without invoking a generation provider."""
        if not self.config.enabled:
            raise RuntimeError("Task-close criteria review is disabled.")

        criteria = split_validation_criteria(validation_criteria)
        if not criteria:
            raise ValueError("Task-close criteria review requires explicit validation criteria.")

        # Gate facts are available to the reviewer at launch; only the
        # deliverable-identifying subset keys the fingerprints, so transcript
        # growth cannot stale a verdict.
        review_policy = {
            "close_review_min_severity": self.config.close_review_min_severity,
            "close_review_max_concurrency_per_project": (
                self.config.close_review_max_concurrency_per_project
            ),
        }
        stable_facts = {
            **stable_checklist_facts(checklist_facts),
            "review_policy": review_policy,
        }
        diff_evidence = build_close_diff_evidence(diff_text, criteria=validation_criteria)
        complete_evidence_sha = diff_evidence.sha256
        test_bodies_sha = hashlib.sha256(test_bodies.encode()).hexdigest()
        evidence_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "diff": complete_evidence_sha,
                    "tests": test_bodies,
                    "facts": stable_facts,
                    "policy": review_policy,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        review_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "task": {
                        "title": title,
                        "description": description,
                        "closure_reason": closure_reason.strip() or "completed",
                        "criteria": criteria,
                        "changes_summary": changes_summary.strip(),
                    },
                    "evidence": {
                        "diff_sha": complete_evidence_sha,
                        "test_bodies_sha": test_bodies_sha,
                        "stable_facts": stable_facts,
                    },
                    "policy": review_policy,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        return PreparedCloseReview(
            criteria=tuple(criteria),
            review_fingerprint=review_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            diff_sha=complete_evidence_sha,
            test_bodies_sha=test_bodies_sha,
            stable_facts=stable_facts,
            manifest_count=diff_evidence.manifest_count,
            excerpt_chars=diff_evidence.excerpt_chars,
        )


__all__ = [
    "TaskValidator",
]
