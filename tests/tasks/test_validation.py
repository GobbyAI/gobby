"""Structured fingerprint contracts for daemon-managed close reviews."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.tasks.validation import PreparedCloseReview, TaskValidator

pytestmark = pytest.mark.unit

_BASE: dict[str, Any] = {
    "title": "Review queued close",
    "description": "Use the linked net patch.",
    "changes_summary": "Implemented durable review queueing.",
    "validation_criteria": "The queue is durable.",
    "diff_text": "diff --git a/src/review.py b/src/review.py\n+queued = True\n",
    "checklist_facts": {
        "commit_count": 1,
        "commit_shas": ["abc123"],
        "had_attributed_edits": True,
        "attributed_paths": ["src/review.py"],
        "validation_commands": {"latest_outcomes": {"test": "success"}},
    },
    "closure_reason": "completed",
    "test_bodies": "def test_queue(): assert queued",
}


def _prepare(
    *,
    config: TaskValidationConfig | None = None,
    **overrides: Any,
) -> PreparedCloseReview:
    arguments = {**_BASE, **overrides}
    return TaskValidator(config or TaskValidationConfig()).prepare_task_review(**arguments)


def test_preparer_has_no_one_shot_generation_path() -> None:
    validator = TaskValidator(TaskValidationConfig())

    assert not hasattr(validator, "validate_task")
    assert not hasattr(validator, "llm_service")


@pytest.mark.parametrize(
    ("override", "moves_evidence"),
    [
        ({"title": "Different title"}, False),
        ({"description": "Different description"}, False),
        ({"changes_summary": "Different summary"}, False),
        ({"validation_criteria": "A different criterion."}, False),
        ({"closure_reason": "obsolete"}, False),
        ({"diff_text": "diff --git a/a.py b/a.py\n+changed = True\n"}, True),
        ({"test_bodies": "def test_other(): assert True"}, True),
        (
            {
                "checklist_facts": {
                    **_BASE["checklist_facts"],
                    "commit_shas": ["def456"],
                }
            },
            True,
        ),
    ],
)
def test_structured_inputs_move_review_fingerprint(
    override: dict[str, Any],
    moves_evidence: bool,
) -> None:
    baseline = _prepare()
    changed = _prepare(**override)

    assert changed.review_fingerprint != baseline.review_fingerprint
    assert (changed.evidence_fingerprint != baseline.evidence_fingerprint) is moves_evidence


def test_additive_transcript_facts_do_not_stale_review() -> None:
    baseline = _prepare()
    changed = _prepare(
        checklist_facts={
            **_BASE["checklist_facts"],
            "validation_commands": {"latest_outcomes": {"test": "success", "lint": "success"}},
            "transcript_operational_actions": ["restart", "cutover"],
            "acceptance_artifacts": ["artifact://new"],
        }
    )

    assert changed.review_fingerprint == baseline.review_fingerprint
    assert changed.evidence_fingerprint == baseline.evidence_fingerprint


@pytest.mark.parametrize(
    "field", ["close_review_min_severity", "close_review_max_concurrency_per_project"]
)
def test_review_policy_change_stales_fingerprints(field: str) -> None:
    baseline = _prepare()
    changed_values = {
        "close_review_min_severity": "high",
        "close_review_max_concurrency_per_project": 4,
    }
    changed = _prepare(config=TaskValidationConfig(**{field: changed_values[field]}))

    assert changed.review_fingerprint != baseline.review_fingerprint
    assert changed.evidence_fingerprint != baseline.evidence_fingerprint
    review_policy = changed.stable_facts["review_policy"]
    assert isinstance(review_policy, dict)
    assert review_policy[field] == changed_values[field]


def test_blank_closure_reason_normalizes_to_completed() -> None:
    assert _prepare(closure_reason=" ").review_fingerprint == _prepare().review_fingerprint


def test_manifest_metadata_covers_every_changed_path() -> None:
    prepared = _prepare(
        diff_text=(
            "diff --git a/src/a.py b/src/a.py\n+a = 1\ndiff --git a/docs/b.md b/docs/b.md\n+# B\n"
        )
    )

    assert prepared.manifest_count == 2
    assert prepared.excerpt_chars > 0
