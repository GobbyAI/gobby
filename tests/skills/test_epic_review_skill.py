"""Contract tests for epic-review lesson recording."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

import pytest

from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "src/gobby/install/shared/skills/epic-review/SKILL.md"
WORKFLOWS = REPO_ROOT / "src/gobby/install/shared/workflows/agents"


class EpicFindingEntry(TypedDict):
    title: str
    check_key: str
    lesson_classes: tuple[Literal["qa-miss", "validation-miss"], ...]
    prevention: str
    leaf_task_ref: str
    path: str
    principle: NotRequired[str]
    root_cause: NotRequired[str]
    confirmed_fix_evidence: NotRequired[str]
    finding_fingerprint: NotRequired[str]


async def test_two_class_epic_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    epic_reviewer = (WORKFLOWS / "epic-reviewer.yaml").read_text(encoding="utf-8")
    qa_reviewer = (WORKFLOWS / "qa-reviewer.yaml").read_text(encoding="utf-8")

    required_contract = (
        "list_check_keys",
        "lesson_classes",
        "principle",
        "root_cause",
        "prevention",
        "leaf_task_ref",
        "confirmed_fix_evidence",
        "epic-qa:<lesson_type>:<check-key>",
        "source_kind=qa_rejection",
        "lesson-domain:code",
        "guardrail_target=checklist",
        "guardrail_target=validation",
        "path tags",
        "Incomplete entries mint nothing",
    )
    for phrase in required_contract:
        assert phrase in skill
    assert "fix confirmed on re-review" in epic_reviewer
    assert "push-injected `qa-miss` lessons" in qa_reviewer
    assert "mandatory first-pass checklist at review start" in qa_reviewer

    monkeypatch.setattr(
        "gobby.review_learning.service._current_project_id",
        lambda: "epic-review-project",
    )
    memories = FakeMemoryManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memories),
        cast(RetirementTaskManager, FakeTaskManager()),
    )
    entries: list[EpicFindingEntry] = [
        {
            "title": "Epic QA caught an unchecked stale section",
            "check_key": "stale-section",
            "lesson_classes": ("qa-miss", "validation-miss"),
            "principle": "Review every changed section against the approved scope",
            "root_cause": "Leaf QA and validation omitted a stale-section check",
            "prevention": "Check changed sections for stale content before approval",
            "leaf_task_ref": "#18700",
            "path": "src/gobby/example.py",
            "confirmed_fix_evidence": "commit abc123; focused test passed",
            "finding_fingerprint": "epic-finding:stale-section",
        },
        {
            "title": "Incomplete finding",
            "check_key": "missing-proof",
            "lesson_classes": ("qa-miss",),
            "prevention": "Require proof",
            "leaf_task_ref": "#18701",
            "path": "src/gobby/incomplete.py",
        },
    ]

    recorded: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for entry in entries:
        for lesson_type in entry["lesson_classes"]:
            guardrail_target = "checklist" if lesson_type == "qa-miss" else "validation"
            finding_fingerprint = entry.get("finding_fingerprint")
            result = await service.record(
                source_kind="qa_rejection",
                source="epic-reviewer",
                source_review="epic-qa:#18699:re-review",
                decision="confirmed",
                finding={
                    "title": entry["title"],
                    "check_key": entry["check_key"],
                    "lesson_type": lesson_type,
                    "pattern_id": f"epic-qa:{lesson_type}:{entry['check_key']}",
                    "finding_fingerprint": (
                        f"{finding_fingerprint}:{lesson_type}" if finding_fingerprint else None
                    ),
                    "principle": entry.get("principle"),
                    "root_cause": entry.get("root_cause"),
                    "prevention": entry["prevention"],
                    "path": entry["path"],
                    "guardrail_target": guardrail_target,
                },
                evidence={
                    "confirmed_fix": entry.get("confirmed_fix_evidence"),
                    "leaf_task_ref": entry["leaf_task_ref"],
                    "files": [entry["path"]],
                },
            )
            if result.get("skipped_reason"):
                skipped.append(result)
            else:
                recorded.append(result)

    assert {result["pattern_id"] for result in recorded} == {
        "epic-qa:qa-miss:stale-section",
        "epic-qa:validation-miss:stale-section",
    }
    assert all("task_ref" not in result for result in recorded)
    assert len(memories.memories) == 2
    assert all("lesson-domain:code" in (memory.tags or []) for memory in memories.memories)
    assert len(skipped) == 1
    assert skipped[0]["skipped_reason"] == "missing_verified_fix"
