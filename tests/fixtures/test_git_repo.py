"""A passing test's throwaway repo is removed at teardown; a failing test's is kept (#25793)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.fixtures.git_repo import init_repo

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe(outcome: str) -> str:
    return textwrap.dedent(
        f"""
        from pathlib import Path

        def test_probe(throwaway_repo: Path) -> None:
            Path(__file__).with_name("repo.txt").write_text(str(throwaway_repo), encoding="utf-8")
            assert (throwaway_repo / ".git").is_dir()
            assert {outcome}
        """
    )


def _run_probe(tmp_path: Path, outcome: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "test_probe.py").write_text(_probe(outcome), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.fixtures.git_repo",
        "--rootdir",
        str(tmp_path),
        "--basetemp",
        str(tmp_path / "basetemp"),
        "test_probe.py",
    ]
    environment = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed, tmp_path / "repo.txt"


def test_a_passing_tests_repo_is_removed_at_teardown(tmp_path: Path) -> None:
    completed, marker = _run_probe(tmp_path, "True")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not Path(marker.read_text(encoding="utf-8")).exists()


def test_a_failing_tests_repo_is_kept_for_the_post_mortem(tmp_path: Path) -> None:
    completed, marker = _run_probe(tmp_path, "False")

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert (Path(marker.read_text(encoding="utf-8")) / ".git").is_dir()


def test_repo_is_created_without_template_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)

    assert not list((repo / ".git" / "hooks").glob("*.sample"))
