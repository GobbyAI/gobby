"""Contract tests for the bundled development-discipline skill."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "src/gobby/install/shared/skills/development-discipline/SKILL.md"


def test_validation_lesson_contract() -> None:
    body = SKILL_PATH.read_text(encoding="utf-8")

    required_contract = (
        "recurring_validation_candidates",
        "exactly one lesson per task",
        "record_review_lesson",
        "source_kind=task_validation",
        'source="task-validation"',
        'source_review="task-validation:<task_uuid>"',
        "build_occurrence_key(source_review, finding_fingerprint)",
        "lesson_type=recurring-validation-failure",
        "list_check_keys",
        "pattern_id=task-validation:recurring-validation-failure:<check-key>",
        "guardrail_target=validation",
        "recurrence count descending",
        "group title ascending",
        "first candidate",
        "failed validation iterations",
        "passing close",
        "prevention",
        "principle",
        "root_cause",
        "file path or symbol",
        "record nothing",
    )

    for phrase in required_contract:
        assert phrase in body
