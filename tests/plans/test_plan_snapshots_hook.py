from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

HOOK_ID = "gobby-plan-snapshots-refresh"
EXPECTED_CALLS = [
    "plan grandfathered-refresh --check",
    "plan legacy-classification-refresh --check",
]
SCOPED_FILES = [
    ".gobby/plans/.grandfathered",
    ".gobby/plans/index.yaml",
    ".gobby/plans/.grandfathered-task-state.yaml",
    ".gobby/plans/.legacy-classification.yaml",
]


def test_hook_rejects_stale_grandfathered_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_hook(tmp_path, monkeypatch, fail_command="grandfathered")

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert calls == EXPECTED_CALLS


def test_hook_rejects_stale_legacy_classification_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_hook(tmp_path, monkeypatch, fail_command="legacy")

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert calls == EXPECTED_CALLS


def test_hook_passes_when_snapshots_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _hook()
    files_pattern = re.compile(hook["files"])

    result, calls = _run_hook(tmp_path, monkeypatch)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert calls == EXPECTED_CALLS
    assert hook["pass_filenames"] is False
    assert all(files_pattern.match(path) for path in SCOPED_FILES)
    assert files_pattern.match(".gobby/plans/task-1.md") is None


def _run_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_command: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "gobby-calls.log"
    _write_fake_gobby(bin_dir / "gobby")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GOBBY_FAKE_LOG", str(log_path))
    monkeypatch.setenv("GOBBY_FAKE_FAIL", fail_command)

    result = subprocess.run(  # noqa: S603
        shlex.split(_hook()["entry"]),
        check=False,
        capture_output=True,
        text=True,
    )

    calls = log_path.read_text(encoding="utf-8").splitlines()
    return result, calls


def _hook() -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == HOOK_ID
    ]
    assert len(hooks) == 1
    return hooks[0]


def _write_fake_gobby(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$GOBBY_FAKE_LOG"
case "$GOBBY_FAKE_FAIL:$*" in
  grandfathered:*"plan grandfathered-refresh --check"*) exit 13 ;;
  legacy:*"plan legacy-classification-refresh --check"*) exit 17 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
