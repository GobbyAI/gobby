"""Focused tests for the run-local sandbox subprocess environment."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from gobby.agents.sandbox_run_environment import SandboxRunPaths


@pytest.mark.parametrize("provider", ["claude", "codex", "grok", "droid", "agy"])
def test_environment_keeps_validation_writes_in_run(provider: str, tmp_path: Path) -> None:
    run = SandboxRunPaths(
        root=tmp_path,
        assets=tmp_path / "assets",
        tmp=tmp_path / "tmp",
        hooks=tmp_path / "hooks",
        logs=tmp_path / "logs",
        cache=tmp_path / "cache",
        shared_cache=tmp_path / "shared",
        cargo_target=tmp_path / "shared" / "target",
    )

    environment = run.environment(provider)
    assert environment["OCR_NO_UPDATE"] == "1"
    assert Path(environment["UV_PROJECT_ENVIRONMENT"]).is_relative_to(run.cache)


def test_reviewer_shell_skips_user_startup_files(tmp_path: Path) -> None:
    shell = shutil.which("zsh")
    if shell is None:
        pytest.skip("zsh is required to check its startup behavior")
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "user-rc-ran"
    (home / ".zshenv").write_text(f": > {shlex.quote(str(marker))}\n")
    run = SandboxRunPaths(
        root=tmp_path / "run",
        assets=tmp_path / "run/assets",
        tmp=tmp_path / "run/tmp",
        hooks=tmp_path / "run/hooks",
        logs=tmp_path / "run/logs",
        cache=tmp_path / "run/cache",
        shared_cache=tmp_path / "shared",
        cargo_target=tmp_path / "shared/target",
    )
    env = {**os.environ, "HOME": str(home), "ZDOTDIR": str(home), **run.environment("codex")}
    result = subprocess.run(
        [shell, "-lc", "exit 0"], env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "reviewer shell sourced the user's .zshenv"
