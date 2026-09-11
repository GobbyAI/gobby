"""Behavioral tests for pre-push report-run integrity manifests."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _run(("git", "init", "-q"), tmp_path)
    _run(("git", "config", "user.email", "tests@gobby.local"), tmp_path)
    _run(("git", "config", "user.name", "Gobby Tests"), tmp_path)
    (tmp_path / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run(("git", "add", "tracked.py"), tmp_path)
    _run(("git", "commit", "-qm", "initial"), tmp_path)
    return tmp_path


def test_manifest_records_identity_commands_and_success(
    repo_root: Path,
    git_repo: Path,
) -> None:
    manifest_path = git_repo / "reports" / "pre-push.json"
    helper = repo_root / "pre-push-manifest.py"
    starting_commit = _run(("git", "rev-parse", "HEAD"), git_repo).stdout.strip()

    _manifest(helper, "start", "--manifest", manifest_path, "--repo-root", git_repo)
    _manifest(
        helper,
        "record",
        "--manifest",
        manifest_path,
        "--name",
        "ruff",
        "--report",
        "reports/ruff.txt",
        "--exit-code",
        "0",
    )
    _manifest(helper, "finish", "--manifest", manifest_path, "--status", "passed")

    manifest = _load_manifest(manifest_path)
    assert manifest["starting_commit"] == starting_commit
    assert manifest["ending_commit"] == starting_commit
    assert manifest["starting_worktree_fingerprint"] == manifest["ending_worktree_fingerprint"]
    assert manifest["status"] == "passed"
    assert manifest["completed_at"] is not None
    assert manifest["commands"] == [
        {
            "completed_at": manifest["commands"][0]["completed_at"],
            "exit_code": 0,
            "gating": True,
            "name": "ruff",
            "report": "reports/ruff.txt",
            "status": "passed",
        }
    ]


@pytest.mark.parametrize(
    "change_source",
    [
        pytest.param(
            lambda repo: (repo / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8"),
            id="tracked",
        ),
        pytest.param(
            lambda repo: (repo / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8"),
            id="untracked",
        ),
    ],
)
def test_manifest_invalidates_tracked_and_untracked_source_drift(
    repo_root: Path,
    git_repo: Path,
    change_source: Callable[[Path], int],
) -> None:
    manifest_path = git_repo / "reports" / "pre-push.json"
    helper = repo_root / "pre-push-manifest.py"
    _manifest(helper, "start", "--manifest", manifest_path, "--repo-root", git_repo)

    change_source(git_repo)
    result = _manifest(
        helper,
        "record",
        "--manifest",
        manifest_path,
        "--name",
        "ruff",
        "--exit-code",
        "0",
        check=False,
    )

    assert result.returncode == 2
    manifest = _load_manifest(manifest_path)
    assert manifest["status"] == "invalidated"
    assert manifest["completed_at"] is not None
    assert manifest["starting_worktree_fingerprint"] != manifest["ending_worktree_fingerprint"]
    assert manifest["invalidation_reasons"] == [
        "tracked or untracked source changed during the report run"
    ]


def test_manifest_records_failed_terminal_status(repo_root: Path, git_repo: Path) -> None:
    manifest_path = git_repo / "reports" / "pre-push.json"
    helper = repo_root / "pre-push-manifest.py"
    _manifest(helper, "start", "--manifest", manifest_path, "--repo-root", git_repo)
    _manifest(
        helper,
        "record",
        "--manifest",
        manifest_path,
        "--name",
        "mypy",
        "--exit-code",
        "1",
    )

    _manifest(helper, "finish", "--manifest", manifest_path, "--status", "failed")

    manifest = _load_manifest(manifest_path)
    assert manifest["status"] == "failed"
    assert manifest["commands"][0]["status"] == "failed"
    assert manifest["commands"][0]["exit_code"] == 1


def test_manifest_invalidates_a_concurrent_head_change(repo_root: Path, git_repo: Path) -> None:
    manifest_path = git_repo / "reports" / "pre-push.json"
    helper = repo_root / "pre-push-manifest.py"
    _manifest(helper, "start", "--manifest", manifest_path, "--repo-root", git_repo)

    _run(("git", "commit", "--allow-empty", "-qm", "concurrent"), git_repo)
    result = _manifest(
        helper,
        "record",
        "--manifest",
        manifest_path,
        "--name",
        "ruff",
        "--exit-code",
        "0",
        check=False,
    )

    assert result.returncode == 2
    manifest = _load_manifest(manifest_path)
    assert manifest["status"] == "invalidated"
    assert manifest["starting_commit"] != manifest["ending_commit"]
    assert manifest["starting_worktree_fingerprint"] == manifest["ending_worktree_fingerprint"]
    assert manifest["invalidation_reasons"] == ["HEAD changed during the report run"]


def test_record_helpers_tolerate_empty_optional_args_under_bash_nounset(
    repo_root: Path, tmp_path: Path
) -> None:
    script = (repo_root / "pre-push-test.sh").read_text(encoding="utf-8")
    assert "gating_args" not in script

    harness = tmp_path / "record-helpers.sh"
    harness.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'MANIFEST_TOOL="pre-push-manifest.py"',
                'MANIFEST_PATH="unused.json"',
                "python3() { :; }",
                _bash_function(script, "record_command_result"),
                _bash_function(script, "record_skipped_command"),
                'record_command_result "ruff" "0" "ruff.txt"',
                'record_skipped_command "coderabbit"',
                'record_command_result "coderabbit" "0" "cr.md" "non-gating"',
                'record_skipped_command "coderabbit" "non-gating"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ("bash", str(harness)),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _bash_function(script: str, name: str) -> str:
    header = f"{name}() {{"
    start = script.index(header)
    end = script.index("\n}\n", start)
    return script[start : end + 3]


def _manifest(
    helper: Path,
    command: str,
    *arguments: str | Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        (sys.executable, str(helper), command, *(str(value) for value in arguments)),
        helper.parent,
        check=check,
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _run(
    command: tuple[str, ...],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )
