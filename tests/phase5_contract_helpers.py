"""Shared helpers for Phase 5 legacy-cutover contract tests."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from gobby.storage.database import LocalDatabase

ROOT = Path(__file__).resolve().parents[1]

NEW_TASK_TYPES = ("simple_fix", "research_spike", "prd_doc", "architecture_doc")

LEGACY_TASK_COLUMNS = ("lifecycle", "lifecycle_stage", "status")
LEGACY_CAP_COLUMNS = (
    "max_expansion_attempts",
    "max_qa_rounds",
    "max_merge_attempts",
    "max_holistic_rounds",
    "max_review_rounds",
)

LEGACY_REVIEW_TOOLS = (
    "mark_task_pr_opened",
    "mark_task_merged",
    "mark_task_merge_failed",
    "advance_lifecycle",
    "mark_task_" + "needs_review",
    "mark_task_review_" + "approved",
    "mark_task_review_" + "rejected",
)


def repo_path(relative: str) -> Path:
    return ROOT / relative


def source_text(relative: str) -> str:
    return repo_path(relative).read_text()


def source_texts(paths: Iterable[str]) -> str:
    chunks: list[str] = []
    for relative in paths:
        path = repo_path(relative)
        if path.is_file():
            chunks.append(path.read_text())
            continue
        for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
            try:
                chunks.append(file_path.read_text())
            except UnicodeDecodeError:
                continue
    return "\n".join(chunks)


def table_columns(db: LocalDatabase, table: str) -> set[str]:
    return {row["name"] for row in db.fetchall(f"PRAGMA table_info({table})")}


def assert_no_regex_matches(
    pattern: str,
    paths: Iterable[str],
    *,
    allowed_paths: Iterable[str] = (),
) -> None:
    regex = re.compile(pattern)
    allowed = {str(repo_path(path)) for path in allowed_paths}
    matches: list[str] = []
    for relative in paths:
        path = repo_path(relative)
        if not path.exists():
            continue
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for file_path in files:
            if str(file_path) in allowed:
                continue
            try:
                text = file_path.read_text()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{file_path.relative_to(ROOT)}:{line_number}:{line.strip()}")
    assert matches == []


def git_grep(pattern: str, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "grep", "-nE", pattern, "--", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
