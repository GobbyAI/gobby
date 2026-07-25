"""Tests for plan evidence resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.cli.plan import _CliEvidenceContext
from gobby.plans.evidence import (
    EvidenceKind,
    EvidenceResolveStatus,
    InvalidEvidenceError,
    resolve_evidence,
)

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


def test_commits_option_ref_is_rejected_before_git(tmp_path: Path) -> None:
    with patch("gobby.plans.evidence.subprocess.run") as run_git:
        with pytest.raises(
            InvalidEvidenceError,
            match="Option-shaped evidence ref '--all' is not allowed",
        ):
            resolve_evidence("commits:--all", ctx=EvidenceContext(tmp_path))

    run_git.assert_not_called()


def test_commits_single_revision_is_rejected_before_git(tmp_path: Path) -> None:
    with patch("gobby.plans.evidence.subprocess.run") as run_git:
        with pytest.raises(
            InvalidEvidenceError,
            match="commits evidence requires an explicit revision range",
        ):
            resolve_evidence("commits:HEAD", ctx=EvidenceContext(tmp_path))

    run_git.assert_not_called()


def test_cli_commit_diff_rejects_option_ref_before_git(tmp_path: Path) -> None:
    ctx = _CliEvidenceContext(repo_root=tmp_path, project_id=None)

    with patch("gobby.cli.plan.subprocess.run") as run_git:
        with pytest.raises(
            InvalidEvidenceError,
            match="Option-shaped evidence ref '--all' is not allowed",
        ):
            ctx.get_commit_range_diff("--all")

    run_git.assert_not_called()


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
    assert bundle.rows[0].status is EvidenceResolveStatus.resolved
    assert bundle.rows[0].artifacts_touched == ("src/new.py",)


@pytest.mark.parametrize("task_diff", ["", "  \n\t"], ids=["empty", "whitespace"])
def test_task_diff_without_content_is_invalid(tmp_path: Path, task_diff: str) -> None:
    bundle = resolve_evidence(
        "task-diff:#13250",
        ctx=EvidenceContext(tmp_path, task_diff=task_diff),
    )

    row = bundle.rows[0]
    assert row.status is EvidenceResolveStatus.invalid
    assert row.artifacts_touched == ()
    assert "link at least one commit" in row.detail


def test_cli_task_diff_with_no_commits_is_invalid(temp_db: Any, tmp_path: Path) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("project")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project.id, "No commits", validation_criteria="Test task completion is observable."
    )
    ctx = _CliEvidenceContext(repo_root=tmp_path, project_id=project.id)
    ctx.__dict__["task_manager"] = manager

    bundle = resolve_evidence(f"task-diff:#{task.seq_num}", ctx=ctx)

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert "link at least one commit" in bundle.rows[0].detail


def test_cli_task_diff_missing_project_is_invalid(tmp_path: Path) -> None:
    bundle = resolve_evidence(
        "task-diff:#13250",
        ctx=_CliEvidenceContext(repo_root=tmp_path, project_id=None),
    )

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert "requires --project-id" in bundle.rows[0].detail


def test_cli_task_diff_unknown_task_is_invalid(temp_db: Any, tmp_path: Path) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("project")
    manager = LocalTaskManager(temp_db)
    ctx = _CliEvidenceContext(repo_root=tmp_path, project_id=project.id)
    ctx.__dict__["task_manager"] = manager

    bundle = resolve_evidence("task-diff:#999999", ctx=ctx)

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert "999999" in bundle.rows[0].detail


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


def test_resolve_coverage_matrix_reads_header_evidence_once(tmp_path: Path) -> None:
    manifest = tmp_path / "coverage.yaml"
    manifest.write_text(
        """\
header:
  evidence:
    - kind: commits
      ref: abc123
      status: resolved
      detail: commit abc123
      artifacts_touched:
        - src/example.py
rows:
  - section_id: A1
    item_id: A1.1
    status: covered
  - section_id: A1
    item_id: A1.2
    status: covered
""",
        encoding="utf-8",
    )

    bundle = resolve_evidence(f"coverage-matrix:{manifest}", ctx=EvidenceContext(tmp_path))

    assert len(bundle.rows) == 1
    assert bundle.rows[0].kind is EvidenceKind.commits
    assert bundle.rows[0].ref == "abc123"
    assert bundle.rows[0].artifacts_touched == ("src/example.py",)


def test_resolve_coverage_matrix_invalid_yaml_raises_invalid_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "coverage.yaml"
    manifest.write_text("rows: [", encoding="utf-8")

    with pytest.raises(InvalidEvidenceError, match="Invalid coverage matrix"):
        resolve_evidence(f"coverage-matrix:{manifest}", ctx=EvidenceContext(tmp_path))


@pytest.mark.parametrize(
    "row_yaml",
    ["covered", "[covered]", "null"],
    ids=["scalar", "list", "null"],
)
def test_resolve_coverage_matrix_rejects_non_mapping_rows(tmp_path: Path, row_yaml: str) -> None:
    manifest = tmp_path / "coverage.yaml"
    manifest.write_text(f"rows:\n  - {row_yaml}\n", encoding="utf-8")

    with pytest.raises(
        InvalidEvidenceError,
        match=r"Invalid coverage matrix .*: row 1 must be a mapping",
    ):
        resolve_evidence(f"coverage-matrix:{manifest}", ctx=EvidenceContext(tmp_path))


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
