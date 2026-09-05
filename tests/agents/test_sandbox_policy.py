from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.agents import sandbox_policy
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _cleanup_pre_commit_store_spare() -> Iterator[None]:
    yield
    sandbox_policy.shutdown_pre_commit_store_spare()


def _workspace(tmp_path: Path, *, configured: bool = True) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if configured:
        (workspace / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return workspace


def _operator_store(path: Path, marker: str) -> Path:
    repo = path / "repoabc"
    repo.mkdir(parents=True)
    (path / "db.db").write_text(f"database:{marker}\n", encoding="utf-8")
    (repo / "hook.py").write_text(f"hook:{marker}\n", encoding="utf-8")
    return path


def _run_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    workspace: Path,
    run_id: str = "run-1",
) -> tuple[sandbox_policy.SandboxRunPaths, Path]:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)
    paths = sandbox_policy.prepare_sandbox_run_paths(
        run_id,
        {},
        workspace=workspace,
    )
    destination = Path(paths.environment("codex")["XDG_CACHE_HOME"]) / "pre-commit"
    return paths, destination


def test_gcode_runtime_write_exception_matches_workspace_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = Path("/Users/josh/.gobby")
    workspace = gobby_home / "worktrees/gobby/task-21329-detach-close-criteria-review"
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)

    assert sandbox_policy.gcode_runtime_write_exceptions(workspace) == [
        str(gobby_home / "gcode-runtime/9717da2af3b3bf43")
    ]


def test_srt_policy_allows_only_current_workspace_gcode_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = Path("/opt/gobby-home")
    runtime_root = gobby_home / "gcode-runtime"
    workspace = gobby_home / "worktrees/gobby/task-21620"
    unrelated_runtime = runtime_root / "unrelated-run"
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)

    paths = compute_sandbox_paths(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        workspace_path=str(workspace),
        provider="codex",
        env={
            "PATH": "",
            "GOBBY_CODE_INDEX_RUNTIME_HOME": str(unrelated_runtime),
        },
    )

    runtime_writes = [path for path in paths.write_paths if Path(path).is_relative_to(runtime_root)]
    assert runtime_writes == sandbox_policy.gcode_runtime_write_exceptions(workspace)
    assert str(unrelated_runtime) not in paths.write_paths
    assert str(runtime_root) in paths.deny_read_paths
    # sandbox-runtime gives denyWrite precedence over allowWrite, so no deny entry
    # may sit at or above the workspace runtime allowance.
    runtime_home = Path(runtime_writes[0])
    assert str(runtime_root) not in paths.deny_write_paths
    assert not any(
        runtime_home == Path(denied) or runtime_home.is_relative_to(denied)
        for denied in paths.deny_write_paths
    )


def test_sensitive_write_roots_exclude_gcode_runtime_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = Path("/opt/gobby-home")
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)

    write_roots = set(sandbox_policy.sensitive_write_roots())

    assert {
        str(gobby_home / "bootstrap.yaml"),
        str(gobby_home / ".secret_kek"),
        str(gobby_home / "local_cli_token"),
        str(gobby_home / "tools" / "srt"),
    } <= write_roots
    assert str(gobby_home / "gcode-runtime") not in write_roots
    assert str(gobby_home / "gcode-runtime") in sandbox_policy.sensitive_roots()


def test_prepare_sandbox_run_paths_copies_writable_isolated_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "selected")
    _operator_store(tmp_path / "xdg" / "pre-commit", "decoy")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    (source / "db.db").chmod(0o400)
    (source / "repoabc" / "hook.py").chmod(0o400)
    (source / "repoabc").chmod(0o500)
    source.chmod(0o500)
    source_modes = {
        path.relative_to(source): stat.S_IMODE(path.stat().st_mode)
        for path in (source, *source.rglob("*"))
    }

    paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    environment = paths.environment("codex")
    assert environment["XDG_CACHE_HOME"] == str(paths.cache / "xdg-cache-home")
    assert "PRE_COMMIT_HOME" not in environment
    assert (destination / "db.db").read_text(encoding="utf-8") == "database:selected\n"
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == ("hook:selected\n")
    assert all(
        path.stat().st_mode & stat.S_IWUSR for path in (destination, *destination.rglob("*"))
    )

    (destination / "db.db").write_text("updated copy\n", encoding="utf-8")
    (destination / ".lock").write_text("", encoding="utf-8")
    (destination / "repoabc" / "generated").write_text("run-owned\n", encoding="utf-8")
    assert (source / "db.db").read_text(encoding="utf-8") == "database:selected\n"
    assert not (source / ".lock").exists()
    assert not (source / "repoabc" / "generated").exists()
    assert source_modes == {
        path.relative_to(source): stat.S_IMODE(path.stat().st_mode)
        for path in (source, *source.rglob("*"))
    }


def test_prepare_sandbox_run_paths_consumes_spare_once_and_replenishes_in_background(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    gobby_home = tmp_path / "gobby-home"
    managed_root = gobby_home / "runtime" / "managed-executions"
    spare, temporary = sandbox_policy.pre_commit_store_spare_paths(managed_root)
    _operator_store(spare, "spare")

    clone_started = threading.Event()
    release_clone = threading.Event()
    original_clone = sandbox_policy._clone_pre_commit_store

    def controlled_clone(clone_source: Path, destination: Path) -> None:
        if destination == temporary:
            original_clone(clone_source, destination)
            clone_started.set()
            assert release_clone.wait(timeout=5)
            return
        original_clone(clone_source, destination)

    rename_calls: list[tuple[Path, Path]] = []
    original_rename = os.rename

    def tracked_rename(source_path: Path, destination: Path) -> None:
        rename_calls.append((Path(source_path), Path(destination)))
        original_rename(source_path, destination)

    monkeypatch.setattr(sandbox_policy, "_clone_pre_commit_store", controlled_clone)
    monkeypatch.setattr(os, "rename", tracked_rename)

    try:
        first_paths, first_destination = _run_cache(
            monkeypatch,
            tmp_path,
            workspace=workspace,
            run_id="run-1",
        )
        assert clone_started.wait(timeout=5)
        worker = sandbox_policy._pre_commit_spare_thread
        assert worker is not None
        assert not spare.exists()
        assert temporary.is_dir()
        assert (first_destination / "db.db").read_text(encoding="utf-8") == ("database:spare\n")

        second_paths, second_destination = _run_cache(
            monkeypatch,
            tmp_path,
            workspace=workspace,
            run_id="run-2",
        )
        assert (second_destination / "db.db").read_text(encoding="utf-8") == ("database:operator\n")
        assert first_paths.root != second_paths.root
        assert rename_calls[0] == (spare, first_destination)
    finally:
        release_clone.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert (spare / "db.db").read_text(encoding="utf-8") == "database:operator\n"
    assert not temporary.exists()


def test_failed_partial_pre_commit_spare_replenish_retries_on_next_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    managed_root = tmp_path / "gobby-home" / "runtime" / "managed-executions"
    spare, temporary = sandbox_policy.pre_commit_store_spare_paths(managed_root)
    original_clone = sandbox_policy._clone_pre_commit_store
    temporary_attempts = 0

    def flaky_clone(clone_source: Path, destination: Path) -> None:
        nonlocal temporary_attempts
        if destination == temporary:
            temporary_attempts += 1
            if temporary_attempts == 1:
                destination.mkdir(parents=True)
                (destination / "partial").write_text("incomplete", encoding="utf-8")
                raise OSError("interrupted clone")
        original_clone(clone_source, destination)

    monkeypatch.setattr(sandbox_policy, "_clone_pre_commit_store", flaky_clone)

    _first_paths, first_destination = _run_cache(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        run_id="run-1",
    )
    worker = sandbox_policy._pre_commit_spare_thread
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert (first_destination / "db.db").is_file()
    assert not spare.exists()
    assert not temporary.exists()

    _second_paths, second_destination = _run_cache(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        run_id="run-2",
    )
    worker = sandbox_policy._pre_commit_spare_thread
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert (second_destination / "db.db").is_file()
    assert (spare / "db.db").read_text(encoding="utf-8") == "database:operator\n"
    assert temporary_attempts == 2


def test_shutdown_pre_commit_store_spare_waits_for_replenish_and_removes_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: gobby_home)
    managed_root = sandbox_policy.managed_execution_root()
    spare, temporary = sandbox_policy.pre_commit_store_spare_paths(managed_root)
    clone_started = threading.Event()
    release_clone = threading.Event()
    shutdown_finished = threading.Event()
    original_clone = sandbox_policy._clone_pre_commit_store

    def controlled_clone(clone_source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        clone_started.set()
        assert release_clone.wait(timeout=5)
        original_clone(clone_source, destination)

    def shutdown() -> None:
        sandbox_policy.shutdown_pre_commit_store_spare()
        shutdown_finished.set()

    monkeypatch.setattr(sandbox_policy, "_clone_pre_commit_store", controlled_clone)
    sandbox_policy._schedule_pre_commit_store_spare(source)
    assert clone_started.wait(timeout=5)
    shutdown_thread = threading.Thread(target=shutdown)
    shutdown_thread.start()

    try:
        assert not shutdown_finished.wait(timeout=0.05)
    finally:
        release_clone.set()
    assert shutdown_finished.wait(timeout=5)
    shutdown_thread.join(timeout=5)
    assert not shutdown_thread.is_alive()
    assert not spare.exists()
    assert not temporary.exists()


def test_prepare_sandbox_run_paths_uses_apfs_clone_on_macos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "cloned")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    monkeypatch.setattr("gobby.agents.sandbox_policy.sys.platform", "darwin")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)
        if command[0] == "/bin/cp":
            shutil.copytree(command[-2], command[-1])

    monkeypatch.setattr("gobby.agents.sandbox_policy.subprocess.run", run)

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert commands[0] == ["/bin/cp", "-c", "-R", str(source), str(destination)]
    assert commands[1] == ["/bin/chmod", "-R", "u+rwX", str(destination)]
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == ("hook:cloned\n")


def test_prepare_sandbox_run_paths_falls_back_when_apfs_clone_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "fallback")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    monkeypatch.setattr("gobby.agents.sandbox_policy.sys.platform", "darwin")

    def run(command: list[str], **_kwargs: object) -> None:
        if command[0] == "/bin/cp":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("gobby.agents.sandbox_policy.subprocess.run", run)

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:fallback\n"
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == ("hook:fallback\n")


def test_prepare_sandbox_run_paths_uses_xdg_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "xdg" / "pre-commit", "xdg")
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(source.parent))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:xdg\n"


def test_prepare_sandbox_run_paths_uses_default_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    home = tmp_path / "home"
    _operator_store(home / ".cache" / "pre-commit", "default")
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:default\n"


def test_prepare_sandbox_run_paths_skips_pre_commit_store_without_workspace_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, configured=False)
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert not destination.exists()


def test_prepare_sandbox_run_paths_skips_missing_operator_pre_commit_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "missing"))

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert not destination.exists()
