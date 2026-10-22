"""Project-scoped Cargo cache: one target and one Cargo home per project."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed Cargo argv in an isolated fixture
from pathlib import Path

import pytest

from gobby.agents import sandbox_policy
from gobby.agents.cargo_environment import (
    cargo_build_env,
    project_cargo_home,
    project_cargo_target_dir,
)
from gobby.agents.cargo_target import (
    cleanup_checkout_cargo_target_dir,
    link_checkout_cargo_target,
)
from gobby.agents.constants import CARGO_HOME, CARGO_TARGET_DIR, get_terminal_env_vars

pytestmark = pytest.mark.integration

PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
OTHER_PROJECT_ID = "9b2e4f10-3c7a-4d8e-b5f1-6a0c2d9e8f71"
PROJECT_NAME = "gobby"
PROBE_BINARY = "gobby-target-probe"


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    return home


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    return path


def _write_probe(checkout: Path, output: str) -> None:
    (checkout / "src").mkdir(parents=True)
    (checkout / "Cargo.toml").write_text(
        f'[package]\nname = "{PROBE_BINARY}"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    (checkout / "src" / "main.rs").write_text(
        f'fn main() {{ println!("{output}"); }}\n',
        encoding="utf-8",
    )


def _build_and_run(checkout: Path, *, build_env: dict[str, str]) -> str:
    env = {**os.environ, **build_env, "CARGO_NET_OFFLINE": "true"}
    subprocess.run(  # nosec B603 - fixed Cargo argv in an isolated fixture
        ["cargo", "build", "--release", "--quiet"],
        cwd=checkout,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    binary = Path(build_env[CARGO_TARGET_DIR]) / "release" / PROBE_BINARY
    run = subprocess.run(  # nosec B603 - binary built by the fixture above
        [str(binary)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return run.stdout.strip()


def test_project_cache_environment_parity(gobby_home: Path, tmp_path: Path) -> None:
    main = _checkout(tmp_path / "gobby")
    task = _checkout(tmp_path / "gobby-task-1")

    target = project_cargo_target_dir(PROJECT_ID, PROJECT_NAME)
    home = project_cargo_home()
    assert target.name == f"cargo-target-{PROJECT_NAME}"
    assert target.parent.name == PROJECT_ID
    assert target.is_relative_to(gobby_home / "cache")
    assert home.is_relative_to(gobby_home / "cache")

    build_env = cargo_build_env(PROJECT_ID, PROJECT_NAME)
    assert build_env[CARGO_TARGET_DIR] == str(target)
    assert build_env[CARGO_HOME] == str(home)

    for checkout in (main, task):
        agent_env = get_terminal_env_vars(
            session_id="sess-1",
            parent_session_id="",
            agent_run_id="run-1",
            project_id=PROJECT_ID,
            checkout_root=checkout,
        )
        assert agent_env[CARGO_TARGET_DIR] == str(target)
        assert agent_env[CARGO_HOME] == str(home)

    run = sandbox_policy.prepare_sandbox_run_paths("run-1", {}, workspace=task)
    sandbox_env = run.environment("claude")
    assert sandbox_env[CARGO_TARGET_DIR] == str(target)
    assert sandbox_env[CARGO_HOME] == str(home)

    other_target = project_cargo_target_dir(OTHER_PROJECT_ID, PROJECT_NAME)
    assert other_target != target
    assert project_cargo_target_dir(PROJECT_ID, "gobby-tools") != target
    assert project_cargo_target_dir(PROJECT_ID, "a b").name == "cargo-target-a-b"


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_divergent_checkout_artifact_provenance(gobby_home: Path, tmp_path: Path) -> None:
    main = tmp_path / "checkout-main"
    feature = tmp_path / "checkout-feature"
    _write_probe(main, "main-source-0000")
    _write_probe(feature, "feature-source-1111")
    build_env = cargo_build_env(PROJECT_ID, PROJECT_NAME)

    assert _build_and_run(main, build_env=build_env) == "main-source-0000"
    assert _build_and_run(feature, build_env=build_env) == "feature-source-1111"
    # The first checkout rebuilds after the second wrote the same release path; a
    # shared target must not report the other checkout's binary as fresh (#22532).
    assert _build_and_run(main, build_env=build_env) == "main-source-0000"
    assert _build_and_run(feature, build_env=build_env) == "feature-source-1111"


def test_worktree_cleanup_preserves_project_cache(gobby_home: Path, tmp_path: Path) -> None:
    main = _checkout(tmp_path / "gobby")
    task = _checkout(tmp_path / "gobby-task-1")
    shared = project_cargo_target_dir(PROJECT_ID, PROJECT_NAME)
    artifact = shared / "release" / PROBE_BINARY
    artifact.parent.mkdir(parents=True)
    artifact.write_text("shared artifact", encoding="utf-8")
    assert link_checkout_cargo_target(main, PROJECT_ID) is True
    assert link_checkout_cargo_target(task, PROJECT_ID) is True

    assert cleanup_checkout_cargo_target_dir(task, PROJECT_ID) is None

    assert artifact.read_text(encoding="utf-8") == "shared artifact"
    assert shared.is_dir()
    assert (main / "target").resolve() == shared.resolve()
    assert not (task / "target").is_symlink()
    assert not (task / "target").exists()
