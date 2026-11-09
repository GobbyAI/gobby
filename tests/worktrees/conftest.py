"""Shared worktree test fixtures."""

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from gobby.worktrees.git._runner import GitRunner

# A loaded host's git query takes this long: longer than the old 5 s deadline, far below any real one.
SLOW_GIT_SECONDS = 8


@pytest.fixture
def loaded_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git queries with a deadline under SLOW_GIT_SECONDS take that long, as on a loaded host."""
    original = GitRunner._run_git

    async def slowed(
        self: GitRunner,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if timeout < SLOW_GIT_SECONDS:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return await original(self, args, cwd=cwd, timeout=timeout, check=check, env=env)

    monkeypatch.setattr(GitRunner, "_run_git", slowed)
