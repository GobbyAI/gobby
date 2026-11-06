"""The build-sweep command sweeps the configured build root and the registered checkouts."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from click.testing import CliRunner

import gobby.cli.build_sweep as build_sweep_cli
from gobby.agents.build_root import configure_build_root
from gobby.agents.cargo_target import checkout_component
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.cli

THREE_DAYS = 3 * 24 * 3600


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    yield home
    configure_build_root(None)


class _FakeRuntime:
    """Stands in for the DB-backed CLI runtime; only the configured build root is read."""

    def __init__(self, build_root: str) -> None:
        self._build_root = build_root

    def require_config(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(agent_build=SimpleNamespace(build_root=self._build_root))

    def require_database(self, **_: object) -> object:
        return object()


def _stale_folder(path: Path) -> Path:
    artifact = path / "debug" / "deps" / "libgobby.rlib"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 64)
    old = time.time() - THREE_DAYS
    for node in [path, *path.rglob("*")]:
        os.utime(node, (old, old), follow_symlinks=False)
    return path


@pytest.fixture
def configured_root(tmp_path: Path) -> Path:
    root = tmp_path / "volume"
    root.mkdir()
    return root


@pytest.fixture
def stale_in_configured_root(configured_root: Path) -> Path:
    return _stale_folder(configured_root / "cargo-target-v2" / "proj-a" / "gone-task")


@pytest.fixture
def stale_in_default_root(gobby_home: Path) -> Path:
    return _stale_folder(gobby_home / "cache" / "cargo-target-v2" / "proj-a" / "gone-task")


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, configured_root: Path) -> _FakeRuntime:
    fake = _FakeRuntime(str(configured_root))
    monkeypatch.setattr(build_sweep_cli, "get_cli_runtime", lambda ctx=None: fake)
    monkeypatch.setattr(build_sweep_cli, "_registered_checkouts", lambda database: [])
    return fake


def test_apply_sweeps_the_configured_root_and_leaves_the_default_alone(
    runtime: _FakeRuntime,
    stale_in_configured_root: Path,
    stale_in_default_root: Path,
) -> None:
    result = CliRunner().invoke(build_sweep_cli.build_sweep, ["--apply"])

    assert result.exit_code == 0, result.output
    assert not stale_in_configured_root.exists()
    assert stale_in_default_root.is_dir()


def test_dry_run_reports_and_removes_nothing(
    runtime: _FakeRuntime, stale_in_configured_root: Path
) -> None:
    result = CliRunner().invoke(build_sweep_cli.build_sweep, [])

    assert result.exit_code == 0, result.output
    assert "would-remove" in result.output
    assert stale_in_configured_root.is_dir()


def test_an_invalid_build_root_fails_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRuntime("relative/build-root")
    monkeypatch.setattr(build_sweep_cli, "get_cli_runtime", lambda ctx=None: fake)

    result = CliRunner().invoke(build_sweep_cli.build_sweep, [])

    assert result.exit_code == 1, result.output
    assert "must be absolute" in result.output
    assert not isinstance(result.exception, ValueError)


def test_json_output_names_each_finding(
    runtime: _FakeRuntime, stale_in_configured_root: Path
) -> None:
    result = CliRunner().invoke(build_sweep_cli.build_sweep, ["--json"])

    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)
    assert {
        "path": str(stale_in_configured_root),
        "action": "would-remove",
        "size_bytes": 64,
        "detail": "",
    } in findings


def test_registered_checkouts_cover_checkouts_worktrees_and_clones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Checkouts:
        def __init__(self, database: object) -> None:
            seen["database"] = database

        def list_for_machine(self, machine_id: str) -> list[SimpleNamespace]:
            seen["machine_id"] = machine_id
            return [SimpleNamespace(root_path="/repo/main")]

    class _Worktrees:
        def __init__(self, database: object) -> None:
            pass

        def list_worktrees(self, *, limit: int | None) -> list[SimpleNamespace]:
            seen["worktree_limit"] = limit
            return [SimpleNamespace(worktree_path="/wt/task-a")]

    class _Clones:
        def __init__(self, database: object) -> None:
            pass

        def list_clones(self, *, limit: int) -> list[SimpleNamespace]:
            seen["clone_limit"] = limit
            return [SimpleNamespace(clone_path="/clone/task-b")]

    monkeypatch.setattr(build_sweep_cli, "require_machine_id", lambda: "machine-a")
    monkeypatch.setattr(build_sweep_cli, "LocalProjectCheckoutManager", _Checkouts)
    monkeypatch.setattr(build_sweep_cli, "LocalWorktreeManager", _Worktrees)
    monkeypatch.setattr(build_sweep_cli, "LocalCloneManager", _Clones)

    # The fake managers ignore the database, so a sentinel stands in for the hub handle.
    database = cast(HubDatabase, "database")
    paths = build_sweep_cli._registered_checkouts(database)

    assert paths == [Path("/repo/main"), Path("/wt/task-a"), Path("/clone/task-b")]
    assert seen["machine_id"] == "machine-a"
    assert seen["worktree_limit"] is None
    clone_limit = seen["clone_limit"]
    assert isinstance(clone_limit, int)
    assert clone_limit > 1000


def test_live_checkout_folders_survive_apply(
    runtime: _FakeRuntime,
    configured_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "checkouts" / "live-task"
    live.mkdir(parents=True)
    live_folder = _stale_folder(
        configured_root / "cargo-target-v2" / "proj-a" / f"{checkout_component(live)}-agent"
    )
    monkeypatch.setattr(build_sweep_cli, "_registered_checkouts", lambda database: [live])

    result = CliRunner().invoke(build_sweep_cli.build_sweep, ["--apply"])

    assert result.exit_code == 0, result.output
    assert live_folder.is_dir()
