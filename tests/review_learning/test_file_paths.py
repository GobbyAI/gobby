from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.file_paths import (
    extract_file_paths_from_mapping,
    extract_lesson_file_paths,
    normalize_lesson_file_path,
    path_tag,
    paths_match,
)
from gobby.review_learning.lessons import CODE_DOMAIN_EXCLUDED_TAGS
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import FakeMemory, FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (
            '  "./src/gobby/review_learning/service.py:42:7"  ',
            "src/gobby/review_learning/service.py",
        ),
        (
            "file:///not/a/gobby/project/src/gobby/tasks.py",
            "",
        ),
        (r"src\gobby\review_learning\service.py", "src/gobby/review_learning/service.py"),
        ("../outside/review.py", ""),
        ("src/gobby/../outside.py", ""),
    ],
)
def test_normalize_lesson_file_path_matrix(value: object, expected: str) -> None:
    assert normalize_lesson_file_path(value) == expected


@pytest.mark.parametrize("value", [None, "", "../outside/review.py", "/not/a/gobby/project.py"])
def test_path_tag_rejects_empty_normalized_paths(value: object) -> None:
    assert path_tag(value) == ""


def test_normalize_lesson_file_path_relativizes_main_and_worktree_roots(tmp_path: Path) -> None:
    main_root = tmp_path / "main"
    worktree_root = tmp_path / "worktree"
    for root in (main_root, worktree_root):
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")

    relative_path = Path("src/gobby/tasks.py")

    assert normalize_lesson_file_path(main_root / relative_path) == relative_path.as_posix()
    assert normalize_lesson_file_path(worktree_root / relative_path) == relative_path.as_posix()


@pytest.mark.parametrize(
    ("touched_path", "lesson_path", "expected"),
    [
        ("src/gobby/tasks.py", "src/gobby/tasks.py", True),
        (
            "vendor/pkg/src/gobby/tasks.py",
            "src/gobby/tasks.py",
            False,
        ),
        ("src/gobby/task_store.py", "src/gobby/tasks.py", False),
        ("../outside/review.py", "src/gobby/review.py", False),
        ("", "src/gobby/tasks.py", False),
    ],
)
def test_paths_match_matrix(touched_path: str, lesson_path: str, expected: bool) -> None:
    assert paths_match(touched_path, lesson_path) is expected


def test_paths_match_canonical_paths_across_checkouts(tmp_path: Path) -> None:
    main_root = tmp_path / "main"
    worktree_root = tmp_path / "worktree"
    for root in (main_root, worktree_root):
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")

    relative_path = Path("src/gobby/tasks.py")

    assert paths_match(main_root / relative_path, worktree_root / relative_path) is True


def test_extract_file_paths_from_nested_mapping_normalizes_and_deduplicates(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".gobby").mkdir(parents=True)
    (repo_root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")
    payload = {
        "file_path": "./src/gobby/tasks.py:15",
        "details": {
            "affected_files": [
                "src/gobby/tasks.py",
                r"src\gobby\sessions.py",
                {"source_path": "../shared/config.py"},
            ],
            "message": "src/gobby/not-a-path-field.py",
        },
        "findings": [{"location": {"path": f"{repo_root}/src/gobby/hooks.py:8:2"}}],
    }

    assert extract_file_paths_from_mapping(payload) == [
        "src/gobby/tasks.py",
        "src/gobby/sessions.py",
        "src/gobby/hooks.py",
    ]


def test_extract_lesson_file_paths_combines_finding_and_evidence_in_order(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    worktree_root = tmp_path / "worktree"
    for root in (main_root, worktree_root):
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")

    finding = {"path": str(worktree_root / "src/gobby/tasks.py")}
    evidence = {
        "files": [
            str(main_root / "src/gobby/tasks.py"),
            "./tests/tasks/test_manager.py",
        ]
    }

    assert extract_lesson_file_paths(finding=finding, evidence=evidence) == [
        "src/gobby/tasks.py",
        "tests/tasks/test_manager.py",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_path", ["tagged", "legacy"])
async def test_plan_lesson_colliding_path_excluded(candidate_path: str) -> None:
    colliding_path = "src/gobby/review_learning/service.py"
    tags = [
        "review-lesson",
        "confirmed",
        *CODE_DOMAIN_EXCLUDED_TAGS,
        "pattern:plan-review:reviewer-miss:correctness-safety:recall-domain",
    ]
    if candidate_path == "tagged":
        tags.append(path_tag(colliding_path))
    memory_manager = FakeMemoryManager()
    memory_manager.memories.append(
        FakeMemory(
            id=f"mem-plan-{candidate_path}",
            content="\n".join(
                [
                    "# Review Lesson",
                    "- pattern_id: plan-review:reviewer-miss:correctness-safety:recall-domain",
                    "- principle: Plan lessons stay in plan recall.",
                    f"- path: {colliding_path}",
                ]
            ),
            tags=tags,
        )
    )
    service = ReviewLearningService(memory_manager, FakeTaskManager())

    result = await service.recall_review_lessons_for_files(
        file_paths=[colliding_path],
        project_id="project",
        limit=1,
    )

    assert result["lessons"] == []


@pytest.mark.asyncio
async def test_file_lesson_recall_fetches_once_and_prioritizes_matching_tags() -> None:
    first_path = "src/gobby/review_learning/service.py"
    second_path = "src/gobby/hooks/session_activation.py"
    legacy = FakeMemory(
        id="mem-legacy",
        content="\n".join(
            [
                "# Review Lesson",
                "- pattern_id: legacy-path-lesson",
                "- principle: Preserve legacy path matching.",
                "- prevention: DO match evidence paths. AVOID dropping legacy lessons.",
                f"- path: {first_path}",
            ]
        ),
        tags=["review-lesson", "confirmed"],
    )
    tagged = FakeMemory(
        id="mem-tagged",
        content="\n".join(
            [
                "# Review Lesson",
                "- pattern_id: tagged-path-lesson",
                "- principle: Prefer directly tagged lessons.",
                "- prevention: DO prioritize matching tags. AVOID per-path queries.",
                f"- path: {second_path}",
            ]
        ),
        tags=["review-lesson", "confirmed", path_tag(second_path)],
    )
    memory_manager = FakeMemoryManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(RetirementTaskManager, FakeTaskManager()),
    )
    list_memories = AsyncMock(return_value=[legacy, tagged, tagged])

    with patch.object(memory_manager, "alist_memories", list_memories):
        result = await service.recall_review_lessons_for_files(
            file_paths=[first_path, second_path],
            project_id="project",
            limit=2,
        )

    assert list_memories.await_count == 1
    await_args = list_memories.await_args
    assert await_args is not None
    assert await_args.kwargs["tags_all"] == ["review-lesson", "confirmed"]
    assert [lesson["memory_id"] for lesson in result["lessons"]] == [
        "mem-tagged",
        "mem-legacy",
    ]
