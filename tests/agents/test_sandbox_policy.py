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
from gobby.agents.cargo_target import checkout_cargo_target_dir
from gobby.agents.constants import shared_agent_cargo_home_dir
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_run_environment import SandboxRunPaths
from gobby.utils.daemon_git import GitFailed

pytestmark = pytest.mark.unit
pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _cleanup_pre_commit_store_spare(tmp_path: Path) -> Iterator[None]:
    yield
    sandbox_policy.shutdown_pre_commit_store_spare()
    # Tests chmod operator stores read-only; leaving them so turns pytest's later
    # basetemp cleanup into concurrent garbage-* sweeps that fail with Errno 66.
    _restore_user_write(tmp_path)


def _restore_user_write(root: Path) -> None:
    # Top-down, so each directory is listable before its children are visited.
    pending = [root]
    while pending:
        directory = pending.pop()
        directory.chmod(directory.stat().st_mode | stat.S_IRWXU)
        pending.extend(
            path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()
        )


def test_read_only_operator_stores_are_writable_after_teardown(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fixture teardown runs after the test body, so an inner session drives it.
    monkeypatch.setattr(sandbox_policy, "shutdown_pre_commit_store_spare", lambda: None)
    pytester.makeconftest(
        "from tests.agents.test_sandbox_policy import _cleanup_pre_commit_store_spare\n"
    )
    pytester.makepyfile(
        """
        from pathlib import Path

        def test_leaves_read_only_store(tmp_path: Path) -> None:
            repo = tmp_path / "operator-pre-commit" / "repoabc"
            repo.mkdir(parents=True)
            (repo / "hook.py").write_text("", encoding="utf-8")
            (repo / "hook.py").chmod(0o400)
            repo.chmod(0o500)
            repo.parent.chmod(0o500)
            Path(__file__).with_name("tree.txt").write_text(str(tmp_path), encoding="utf-8")
        """
    )

    pytester.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio").assert_outcomes(
        passed=1
    )

    tree = Path((pytester.path / "tree.txt").read_text(encoding="utf-8"))
    assert all(
        path.stat().st_mode & stat.S_IWUSR for path in (tree, *tree.rglob("*")) if path.is_dir()
    )
    shutil.rmtree(tree)
    assert not tree.exists()


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
) -> tuple[SandboxRunPaths, Path]:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
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
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    assert sandbox_policy.gcode_runtime_write_exceptions(workspace) == [
        str(gobby_home / "gcode-runtime/9717da2af3b3bf43")
    ]


@pytest.mark.asyncio
async def test_srt_policy_allows_only_current_workspace_gcode_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = Path("/opt/gobby-home")
    runtime_root = gobby_home / "gcode-runtime"
    workspace = gobby_home / "worktrees/gobby/task-21620"
    unrelated_runtime = runtime_root / "unrelated-run"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    async def no_git_metadata(*_args: object, **_kwargs: object) -> GitFailed:
        return GitFailed(
            status="failed",
            argv=("git", "rev-parse"),
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr("gobby.agents.sandbox.daemon_git.run", no_git_metadata)

    paths = await compute_sandbox_paths(
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
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

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
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
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

    monkeypatch.setattr("gobby.agents.sandbox_policy.spawn.run", run)

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

    monkeypatch.setattr("gobby.agents.sandbox_policy.spawn.run", run)

    _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert (destination / "db.db").read_text(encoding="utf-8") == "database:fallback\n"
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == ("hook:fallback\n")


def test_failed_apfs_clone_is_replaced_by_a_symlink_preserving_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "partial")
    interpreter = tmp_path / "interpreter"
    interpreter.write_text("binary\n", encoding="utf-8")
    interpreter.chmod(0o500)
    venv_bin = source / "repoabc" / "py_env" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(interpreter)
    (source / "repoabc" / "hook.py").chmod(0o400)
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    monkeypatch.setattr("gobby.agents.sandbox_policy.sys.platform", "darwin")

    def run(command: list[str], **_kwargs: object) -> None:
        if command[0] == "/bin/cp":
            # cp gets as far as the read-only object and the venv symlink, then fails.
            shutil.copytree(command[-2], command[-1], symlinks=True)
            raise subprocess.CalledProcessError(1, command, stderr="cp: clonefile failed\n")
        raise OSError("chmod unavailable")

    monkeypatch.setattr("gobby.agents.sandbox_policy.spawn.run", run)

    with caplog.at_level("WARNING", logger="gobby.agents.sandbox_policy"):
        _paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert "cp: clonefile failed" in caplog.text
    assert (destination / "repoabc" / "hook.py").read_text(encoding="utf-8") == "hook:partial\n"
    python3 = destination / "repoabc" / "py_env" / "bin" / "python3"
    assert python3.is_symlink()
    assert python3.readlink() == interpreter
    assert stat.S_IMODE(interpreter.stat().st_mode) == 0o500
    assert all(
        path.lstat().st_mode & stat.S_IWUSR
        for path in (destination, *destination.rglob("*"))
        if not path.is_symlink()
    )


def test_failed_pre_commit_prewarm_leaves_no_partial_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "broken")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))

    def failing_clone(_source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        (destination / "db.db").write_text("partial\n", encoding="utf-8")
        (destination / "db.db").chmod(0o400)
        destination.chmod(0o500)
        raise shutil.Error("copy failed")

    monkeypatch.setattr(sandbox_policy, "_clone_pre_commit_store", failing_clone)
    monkeypatch.setattr(sandbox_policy, "_schedule_pre_commit_store_spare", lambda _source: None)

    with caplog.at_level("WARNING", logger="gobby.agents.sandbox_policy"):
        paths, destination = _run_cache(monkeypatch, tmp_path, workspace=workspace)

    assert "Failed to prewarm pre-commit store" in caplog.text
    assert not destination.exists()
    assert paths.cache.is_dir()


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


def test_sandboxed_runs_share_cargo_caches_that_unsandboxed_builds_never_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cargo fingerprints embed $CARGO_HOME/registry/src, so a per-run home rebuilds
    every dependency on each run, and a target shared with a build under another home
    rebuilds on every alternation. Sandboxed runs share their own stable pair, under a
    write grant that reaches nothing an unsandboxed build reads or executes (#23194)."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    workspace = _workspace(tmp_path)
    unsandboxed = {
        "CARGO_HOME": shared_agent_cargo_home_dir(),
        "CARGO_TARGET_DIR": checkout_cargo_target_dir(workspace, "project-1"),
    }

    first, _destination = _run_cache(monkeypatch, tmp_path, workspace=workspace, run_id="run-1")
    second, _destination = _run_cache(monkeypatch, tmp_path, workspace=workspace, run_id="run-2")

    first_env = first.environment("claude")
    second_env = second.environment("codex")
    for name, unsandboxed_path in unsandboxed.items():
        sandboxed_path = Path(first_env[name])
        assert sandboxed_path != unsandboxed_path
        assert Path(second_env[name]) == sandboxed_path
        assert sandboxed_path.is_dir()
        assert not sandboxed_path.is_relative_to(first.cache)
        assert sandboxed_path.is_relative_to(first.shared_cache)
        assert not unsandboxed_path.is_relative_to(first.shared_cache)
        assert not first.shared_cache.is_relative_to(unsandboxed_path)
    assert second.shared_cache == first.shared_cache


def _listing(path: Path) -> list[str] | None:
    return sorted(child.name for child in path.iterdir()) if path.exists() else None


@pytest.mark.parametrize("link_to", ["unsandboxed", "missing"], ids=["existing", "dangling"])
@pytest.mark.parametrize("entry", ["cargo_home", "cargo_target"])
def test_run_paths_refuse_a_sandbox_cache_entry_linked_outside(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry: str,
    link_to: str,
) -> None:
    """A run may write its cargo home and target entries, so it can swap either for a
    link into a cache unsandboxed builds use. The next run must refuse the link rather
    than create through it or grant it (#23194)."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    workspace = _workspace(tmp_path)
    first, _destination = _run_cache(monkeypatch, tmp_path, workspace=workspace, run_id="run-1")
    unsandboxed = tmp_path / "unsandboxed"
    unsandboxed.mkdir()
    (unsandboxed / "config.toml").write_text("unsandboxed\n", encoding="utf-8")
    planted = getattr(first, entry)
    shutil.rmtree(planted)
    planted.symlink_to(tmp_path / link_to, target_is_directory=True)

    with pytest.raises(PermissionError, match="resolves outside the sandbox cache"):
        _run_cache(monkeypatch, tmp_path, workspace=workspace, run_id="run-2")

    assert planted.is_symlink()
    assert _listing(unsandboxed) == ["config.toml"]
    assert _listing(tmp_path / "missing") is None


def test_run_paths_refuse_a_sandbox_cache_root_linked_outside(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared root itself must not lead out of <gobby-home>/cache/sandbox (#23194)."""
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (gobby_home / "cache").mkdir(parents=True)
    (gobby_home / "cache" / "sandbox").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PermissionError, match="resolves outside the sandbox cache"):
        _run_cache(monkeypatch, tmp_path, workspace=_workspace(tmp_path))

    assert _listing(outside) == []


def test_ghostty_dependency_host_grant() -> None:
    ghostty_host = "deps.files.ghostty.org"
    control_host = "example.com"

    enabled_domains = sandbox_policy.allowed_domains(
        SandboxConfig(enabled=True, backend="srt", allow_package_registries=True),
        provider=None,
        api_base=None,
    )
    disabled_domains = sandbox_policy.allowed_domains(
        SandboxConfig(enabled=True, backend="srt", allow_package_registries=False),
        provider=None,
        api_base=None,
    )

    assert ghostty_host in enabled_domains
    assert control_host not in enabled_domains
    assert ghostty_host not in disabled_domains
    assert control_host not in disabled_domains


def test_managed_grant_lock_path_names_the_lock_beside_the_run_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """gcode takes `<grant>.lock`, and that file lands in the read-only run root.

    Only the run root's four siblings are writable, so the lock needs its own
    grant. Both lock call sites in crates/gcore/src/grant/acquisition.rs
    propagate an IO error rather than reading it as "lock unavailable", so an
    ungranted lock fails the refresh with EPERM instead of making it wait.
    """
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    run_root = sandbox_policy.managed_execution_root() / "1c9d0c7e"
    run_root.mkdir(parents=True)

    lock = sandbox_policy.managed_grant_lock_path(
        {"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(run_root / "grant.json")}
    )

    assert lock == run_root / "grant.json.lock"
    # The root itself stays ungranted: it also holds grant.json and bootstrap.json.
    assert lock is not None
    assert lock.parent == run_root


def test_managed_grant_lock_path_is_absent_without_a_managed_bootstrap() -> None:
    """An unmanaged run has no grant file, so there is no lock to grant."""
    assert sandbox_policy.managed_grant_lock_path({}) is None
    assert sandbox_policy.managed_grant_lock_path({"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": ""}) is None


def test_managed_grant_lock_path_refuses_a_bootstrap_outside_the_managed_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The environment is attacker-adjacent, so the grant never leaves the root.

    `prepare_sandbox_run_paths` already ignores a bootstrap that does not live
    under the managed-execution root; the write grant must agree, or a forged
    variable would open an arbitrary path for writing.
    """
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert (
        sandbox_policy.managed_grant_lock_path(
            {"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(elsewhere / "grant.json")}
        )
        is None
    )


def test_prepare_sandbox_run_paths_skips_pre_commit_prewarm_when_definition_opts_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = _operator_store(tmp_path / "operator-pre-commit", "operator")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(source))
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    spare, _temporary = sandbox_policy.pre_commit_store_spare_paths()
    _operator_store(spare, "spare")
    spare_inode = spare.stat().st_ino
    clones: list[Path] = []
    schedules: list[Path] = []
    monkeypatch.setattr(
        sandbox_policy,
        "_clone_pre_commit_store",
        lambda _source, destination: clones.append(destination),
    )
    monkeypatch.setattr(sandbox_policy, "_schedule_pre_commit_store_spare", schedules.append)

    paths = sandbox_policy.prepare_sandbox_run_paths(
        "run-1",
        {},
        workspace=workspace,
        prewarm_pre_commit_store=False,
    )

    destination = Path(paths.environment("codex")["XDG_CACHE_HOME"]) / "pre-commit"
    assert not destination.exists()
    assert spare.stat().st_ino == spare_inode
    assert (spare / "db.db").read_text(encoding="utf-8") == "database:spare\n"
    assert clones == []
    assert schedules == []
    assert sandbox_policy._pre_commit_spare_thread is None


@pytest.mark.asyncio
async def test_break_glass_file_is_a_credential_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils import local_token

    home = tmp_path / "home"
    workspace = tmp_path / "allowed"
    workspace.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    bootstrap = workspace / "bootstrap.yaml"
    local_token.bind_daemon_bootstrap(bootstrap)

    async def no_git_metadata(*_args: object, **_kwargs: object) -> GitFailed:
        return GitFailed(
            status="failed",
            argv=("git", "rev-parse"),
            returncode=128,
            stdout="",
            stderr="not a git repository",
        )

    monkeypatch.setattr("gobby.agents.sandbox.daemon_git.run", no_git_metadata)
    for path in (bootstrap, workspace / "break_glass", workspace / ".break_glass-staging"):
        assert str(path) in sandbox_policy.sensitive_roots()
        assert str(path) in sandbox_policy.sensitive_write_roots()
    with pytest.raises(ValueError, match="sandbox allow path contains sensitive root"):
        await compute_sandbox_paths(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            workspace_path=str(workspace),
            provider="codex",
            env={"PATH": ""},
        )
