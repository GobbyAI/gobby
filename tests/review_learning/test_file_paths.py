from __future__ import annotations

import pytest

from gobby.review_learning.file_paths import (
    extract_file_paths_from_mapping,
    extract_lesson_file_paths,
    normalize_lesson_file_path,
    paths_match,
)

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
            "file:///Users/josh/Projects/gobby/src/gobby/tasks.py",
            "Users/josh/Projects/gobby/src/gobby/tasks.py",
        ),
        (
            "/Users/josh/.gobby/worktrees/gobby/review-fixes/src/gobby/tasks.py",
            "Users/josh/.gobby/worktrees/gobby/review-fixes/src/gobby/tasks.py",
        ),
        (r"src\gobby\review_learning\service.py", "src/gobby/review_learning/service.py"),
        ("../outside/review.py", "../outside/review.py"),
    ],
)
def test_normalize_lesson_file_path_matrix(value: object, expected: str) -> None:
    assert normalize_lesson_file_path(value) == expected


@pytest.mark.parametrize(
    ("touched_path", "lesson_path", "expected"),
    [
        ("src/gobby/tasks.py", "src/gobby/tasks.py", True),
        (
            "/Users/josh/.gobby/worktrees/gobby/review-fixes/src/gobby/tasks.py",
            "src/gobby/tasks.py",
            True,
        ),
        ("/Users/josh/Projects/gobby/src/gobby/tasks.py", "src/gobby/tasks.py", True),
        ("packages/api/src/gobby/tasks.py", "src/gobby/tasks.py", True),
        ("src/gobby/task_store.py", "src/gobby/tasks.py", False),
        ("../outside/review.py", "src/gobby/review.py", False),
        ("", "src/gobby/tasks.py", False),
    ],
)
def test_paths_match_matrix(touched_path: str, lesson_path: str, expected: bool) -> None:
    assert paths_match(touched_path, lesson_path) is expected


def test_extract_file_paths_from_nested_mapping_normalizes_and_deduplicates() -> None:
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
        "findings": [{"location": {"path": "/Users/josh/Projects/gobby/src/gobby/hooks.py:8:2"}}],
    }

    assert extract_file_paths_from_mapping(payload) == [
        "src/gobby/tasks.py",
        "src/gobby/sessions.py",
        "../shared/config.py",
        "Users/josh/Projects/gobby/src/gobby/hooks.py",
    ]


def test_extract_lesson_file_paths_combines_finding_and_evidence_in_order() -> None:
    finding = {"path": "/Users/josh/.gobby/worktrees/gobby/review-fixes/src/gobby/tasks.py"}
    evidence = {
        "files": [
            "/Users/josh/Projects/gobby/src/gobby/tasks.py",
            "./tests/tasks/test_manager.py",
        ]
    }

    assert extract_lesson_file_paths(finding=finding, evidence=evidence) == [
        "Users/josh/.gobby/worktrees/gobby/review-fixes/src/gobby/tasks.py",
        "Users/josh/Projects/gobby/src/gobby/tasks.py",
        "tests/tasks/test_manager.py",
    ]
