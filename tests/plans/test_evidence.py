"""Tests for plan evidence resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gobby.plans.evidence import EvidenceKind, EvidenceResolveStatus, resolve_evidence

pytestmark = pytest.mark.unit


@dataclass
class EvidenceContext:
    repo_root: Path
    task_diff: str = ""
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    commit_range_diff: str = ""

    def get_task_diff(self, task_ref: str) -> str:
        assert task_ref == "#13250"
        return self.task_diff

    def get_artifacts(self, task_ref: str) -> dict[str, Any] | None:
        return self.artifacts.get(task_ref)

    def get_commit_range_diff(self, range_: str) -> str:
        return self.commit_range_diff


def test_resolve_commits_range(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    base = _commit_file(repo, "base.txt", "base\n", "base")
    first = _commit_file(repo, "first.txt", "first\n", "first")
    second = _commit_file(repo, "nested/second.txt", "second\n", "second")

    bundle = resolve_evidence(f"commits:{base}..{second}", ctx=EvidenceContext(repo))

    assert [row.ref for row in bundle.rows] == [first, second]
    assert [row.artifacts_touched for row in bundle.rows] == [
        ("first.txt",),
        ("nested/second.txt",),
    ]
    assert all(row.status is EvidenceResolveStatus.resolved for row in bundle.rows)


def test_resolve_task_diff(tmp_path: Path) -> None:
    diff = """\
diff --git a/src/old.py b/src/new.py
index 1111111..2222222 100644
--- a/src/old.py
+++ b/src/new.py
@@ -1 +1 @@
-old
+new
"""

    bundle = resolve_evidence(
        "task-diff:#13250",
        ctx=EvidenceContext(tmp_path, task_diff=diff),
    )

    assert bundle.rows[0].kind is EvidenceKind.task_diff
    assert bundle.rows[0].artifacts_touched == ("src/new.py",)


def test_resolve_coverage_matrix(tmp_path: Path) -> None:
    manifest = tmp_path / "coverage.yaml"
    manifest.write_text(
        """\
rows:
  - section_id: A6
    item_id: A6.5
    status: covered
    detail: ok
    leaves:
      - leaf_task_ref: "#13253"
        validation_criteria_snippet: fixture
        matched_artifact_ref: tests/plans/test_evidence.py
  - section_id: A6
    item_id: A6.bad
    status: invalid
    detail: broken row
""",
        encoding="utf-8",
    )

    bundle = resolve_evidence(f"coverage-matrix:{manifest}", ctx=EvidenceContext(tmp_path))

    assert [row.ref for row in bundle.rows] == ["A6:A6.5", "A6:A6.bad"]
    assert bundle.rows[0].status is EvidenceResolveStatus.resolved
    assert bundle.rows[0].artifacts_touched == ("tests/plans/test_evidence.py",)
    assert bundle.rows[1].status is EvidenceResolveStatus.invalid


def test_resolve_none_emits_audit_row(tmp_path: Path) -> None:
    bundle = resolve_evidence("none", ctx=EvidenceContext(tmp_path))

    assert len(bundle.rows) == 1
    assert bundle.rows[0].kind is EvidenceKind.none
    assert bundle.rows[0].status is EvidenceResolveStatus.resolved
    assert bundle.rows[0].detail == "explicit operator override"


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "a6@example.test")
    _git(repo, "config", "user.name", "A6")
    return repo


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> str:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
