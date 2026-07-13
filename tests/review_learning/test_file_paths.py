from __future__ import annotations

from pathlib import Path

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
