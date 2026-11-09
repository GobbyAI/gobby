"""A throwaway git repo per test (#25793).

Each repo is created without git's default template, whose hook samples add about
fourteen files to every repo. A test that passes takes its repo tree with it at
teardown. A failing test keeps its tree so the post-mortem has the files.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "--template="], cwd=path, check=True, capture_output=True)


@pytest.fixture
def throwaway_repo(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    failures_before = request.session.testsfailed
    repo = tmp_path / "repo"
    init_repo(repo)
    yield repo
    # Failures counted since setup include this test's own call phase.
    if request.session.testsfailed == failures_before:
        shutil.rmtree(repo)
