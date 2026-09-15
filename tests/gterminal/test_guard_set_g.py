"""Unit tests for the executable Guard set G runner."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from gobby.guard_set_g import (
    REQUIRED_GATED_TARGETS,
    GuardSetGError,
    _isolated_child_env,
    check_hosts,
    client_clippy_argv,
    client_nextest_argv,
    evaluate_gated_targets,
    gterm_socket_path_budget,
    isolated_run_root,
    isolated_run_root_name_budget,
    isolated_run_roots,
    isolated_temp_parent,
    leaked_hosts,
    path_under,
    run_group,
    snapshot_gterm_hosts,
    socket_dir_from_cmdline,
    terminal_clippy_argv,
    terminal_nextest_list_argv,
    terminal_nextest_run_argv,
    wrap_command,
)

pytestmark = pytest.mark.unit


def _host_cmd(socket_dir: Path) -> list[str]:
    return ["/usr/local/bin/gterm", "host", "--socket-dir", str(socket_dir)]


def _suite(
    binary_name: str,
    *,
    tests: Sequence[str] = ("one",),
    skip: bool = False,
) -> dict[str, Any]:
    return {
        "binary-id": f"gobby-terminal::{binary_name}",
        "binary-name": binary_name,
        "package-name": "gobby-terminal",
        "kind": "test",
        "testcases": {
            name: {"status": "skipped" if skip else "listed", "skip": skip} for name in tests
        },
    }


def _list_payload(binaries: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    return {"rust-suites": {f"gobby-terminal::{name}": suite for name, suite in binaries.items()}}


def _complete_gated_payload() -> dict[str, Any]:
    return _list_payload({name: _suite(name) for name in sorted(REQUIRED_GATED_TARGETS)})


class ScriptedSnapshot:
    def __init__(self, frames: Sequence[Mapping[int, Path]]) -> None:
        self._frames = [dict(frame) for frame in frames]
        self.calls = 0

    def __call__(self) -> dict[int, Path]:
        index = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return dict(self._frames[index])


class RecordingFinalize:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Path, tuple[Path, ...]]] = []

    def __call__(self, pid: int, socket_dir: Path, roots: tuple[Path, ...]) -> None:
        self.calls.append((pid, socket_dir, roots))


def test_socket_dir_from_cmdline_requires_gterm_host_and_socket_dir(tmp_path: Path) -> None:
    socket_dir = tmp_path / "sock"
    assert socket_dir_from_cmdline(_host_cmd(socket_dir)) == socket_dir.resolve()
    assert socket_dir_from_cmdline(["gterm", "gate", "--socket-dir", str(socket_dir)]) is None
    assert socket_dir_from_cmdline(["gterm", "host"]) is None
    assert socket_dir_from_cmdline(["not-gterm", "host", "--socket-dir", str(socket_dir)]) is None


def test_snapshot_maps_host_pid_to_resolved_socket_dir(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    hosts = snapshot_gterm_hosts(
        [
            (11, _host_cmd(owned)),
            (12, ["gterm", "gate", "--socket-dir", str(other)]),
            (13, ["bash", "-lc", "sleep 1"]),
        ]
    )
    assert hosts == {11: owned.resolve()}


def test_leaked_hosts_are_new_or_moved_under_run_roots(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    durable = tmp_path / "home" / ".gobby" / "gterm"
    durable.mkdir(parents=True)
    owned = run_root / "sock"
    owned.mkdir()
    before = {57123: durable.resolve()}
    after = {57123: durable.resolve(), 99: owned.resolve()}
    assert leaked_hosts(before, after, (run_root.resolve(),)) == {99: owned.resolve()}
    assert leaked_hosts(before, before, (run_root.resolve(),)) == {}


def test_unrelated_host_outside_run_roots_is_not_a_leak(tmp_path: Path) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    durable = (tmp_path / "home" / ".gobby" / "gterm").resolve()
    durable.mkdir(parents=True)
    stranger = (tmp_path / "tmp" / "other-session").resolve()
    stranger.mkdir(parents=True)
    before = {57123: durable}
    after = {57123: durable, 4242: stranger}
    assert leaked_hosts(before, after, (run_root,)) == {}
    assert path_under(durable, run_root) is False
    assert path_under(stranger, run_root) is False


def test_wrap_records_deliberate_leak_and_prints_pid_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    durable = (tmp_path / "home" / ".gobby" / "gterm").resolve()
    durable.mkdir(parents=True)
    leaked = (run_root / "sock").resolve()
    leaked.mkdir()
    snapshot = ScriptedSnapshot(
        [
            {57123: durable},
            {57123: durable, 88: leaked},
            {57123: durable, 88: leaked},
        ]
    )
    finalize = RecordingFinalize()
    outcome = wrap_command(
        ["false"],
        run_roots=(run_root,),
        runner=lambda argv, env: 0,
        snapshot=snapshot,
        finalize=finalize,
    )
    captured = capsys.readouterr()
    assert outcome.command_returncode == 0
    assert outcome.leak_recorded is True
    assert outcome.remaining == {88: leaked}
    assert outcome.exit_code != 0
    assert finalize.calls == [(88, leaked, (run_root,))]
    assert "pid=88" in captured.err
    assert str(leaked) in captured.err
    assert 57123 not in {pid for pid, _socket, _roots in finalize.calls}


def test_wrap_does_not_finalize_unrelated_host(tmp_path: Path) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    durable = (tmp_path / "home" / ".gobby" / "gterm").resolve()
    durable.mkdir(parents=True)
    snapshot = ScriptedSnapshot([{57123: durable}, {57123: durable}, {57123: durable}])
    finalize = RecordingFinalize()
    outcome = wrap_command(
        ["true"],
        run_roots=(run_root,),
        runner=lambda argv, env: 0,
        snapshot=snapshot,
        finalize=finalize,
    )
    assert outcome.exit_code == 0
    assert outcome.leak_recorded is False
    assert finalize.calls == []
    assert outcome.ended_durable == {}


def test_failure_path_still_runs_after_check_and_cleanup(tmp_path: Path) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    durable = (tmp_path / "home" / ".gobby" / "gterm").resolve()
    durable.mkdir(parents=True)
    leaked = (run_root / "sock").resolve()
    leaked.mkdir()
    snapshot = ScriptedSnapshot(
        [
            {57123: durable},
            {57123: durable, 77: leaked},
            {57123: durable},
        ]
    )
    finalize = RecordingFinalize()
    ran: list[str] = []

    def runner(argv: Sequence[str], env: Mapping[str, str]) -> int:
        del env
        ran.append(argv[0])
        return 17

    outcome = wrap_command(
        ["cargo", "nextest", "run"],
        run_roots=(run_root,),
        runner=runner,
        snapshot=snapshot,
        finalize=finalize,
    )
    assert ran == ["cargo"]
    assert snapshot.calls >= 3
    assert finalize.calls == [(77, leaked, (run_root,))]
    assert outcome.command_returncode == 17
    assert outcome.leak_recorded is True
    assert outcome.remaining == {}
    assert outcome.exit_code == 17


def test_cleanup_does_not_erase_recorded_leak_failure(tmp_path: Path) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    leaked = (run_root / "sock").resolve()
    leaked.mkdir()
    snapshot = ScriptedSnapshot([{}, {42: leaked}, {}])
    outcome = wrap_command(
        ["ok"],
        run_roots=(run_root,),
        runner=lambda argv, env: 0,
        snapshot=snapshot,
        finalize=RecordingFinalize(),
    )
    assert outcome.remaining == {}
    assert outcome.leak_recorded is True
    assert outcome.exit_code != 0


def test_check_hosts_fails_on_surviving_owned_host_and_preserves_durable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    durable = (tmp_path / "home" / ".gobby" / "gterm").resolve()
    durable.mkdir(parents=True)
    owned = (run_root / "sock").resolve()
    owned.mkdir()
    outcome = check_hosts(
        run_roots=(run_root,),
        snapshot=lambda: {57123: durable, 9: owned},
        finalize=RecordingFinalize(),
    )
    captured = capsys.readouterr()
    assert outcome.exit_code != 0
    assert outcome.remaining == {9: owned}
    assert "pid=9" in captured.err
    assert "pid=57123" in captured.err
    assert "durable" in captured.err


def test_isolated_temp_parent_keeps_gterm_socket_under_sockaddr_limit() -> None:
    parent = isolated_temp_parent()
    name_budget = isolated_run_root_name_budget(parent)
    if name_budget >= 1:
        run_root = parent / ("x" * min(name_budget, 12))
    else:
        run_root = parent
    assert gterm_socket_path_budget(run_root) < 104


def test_gterm_socket_path_budget_detects_oversize_run_root() -> None:
    run_root = Path("/var/folders") / ("x" * 80) / "run"
    assert gterm_socket_path_budget(run_root) >= 104


def test_isolated_run_root_stays_under_sockaddr_limit() -> None:
    with isolated_run_root() as run_root:
        assert gterm_socket_path_budget(run_root) < 104
        assert run_root.is_dir()


def test_isolated_run_root_reuses_parent_when_it_already_fits() -> None:
    parent = isolated_temp_parent()
    if gterm_socket_path_budget(parent) >= 104:
        pytest.skip("writable temp parent itself exceeds the sockaddr_un budget")
    with isolated_run_root(parent) as run_root:
        assert run_root == parent.resolve()
        assert gterm_socket_path_budget(run_root) < 104


def test_host_wrap_group_fails_closed_when_temp_parent_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> Path:
        raise GuardSetGError("no writable temp parent")

    monkeypatch.setattr("gobby.guard_set_g.isolated_temp_parent", boom)
    monkeypatch.setattr("gobby.guard_set_g.print_installed_provenance", lambda: None)
    assert run_group(2) == 1


def test_isolated_child_env_keeps_explicit_zig_cache(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    env = _isolated_child_env(
        run_root,
        {
            "PATH": "/bin",
            "ZIG_GLOBAL_CACHE_DIR": "/custom/zig",
            "LIBGHOSTTY_VT_ZIG_SYSTEM_DIR": "/custom/zig/p",
        },
    )
    assert env["GOBBY_GUARD_SET_G_RUN_ROOT"] == str(run_root)
    assert env["TMPDIR"] == str(run_root)
    assert env["CLAUDE_CODE_TMPDIR"] == str(run_root)
    assert env["ZIG_GLOBAL_CACHE_DIR"] == "/custom/zig"
    assert env["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] == "/custom/zig/p"


def test_isolated_child_env_uses_vendored_zig_cache_when_unset(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    repo = tmp_path / "repo"
    vendor_cache = repo / "crates" / "gterminal" / "vendor" / "libghostty-vt" / ".zig-cache"
    env = _isolated_child_env(run_root, {"PATH": "/bin"}, repo=repo)
    assert env["ZIG_GLOBAL_CACHE_DIR"] == str(vendor_cache)
    assert vendor_cache.is_dir()


def test_isolated_run_roots_read_env_paths(tmp_path: Path) -> None:
    run_root = tmp_path / "guard-root"
    claude_tmp = tmp_path / "claude-tmp"
    run_root.mkdir()
    claude_tmp.mkdir()
    roots = isolated_run_roots(
        {
            "GOBBY_GUARD_SET_G_RUN_ROOT": str(run_root),
            "CLAUDE_CODE_TMPDIR": str(claude_tmp),
        }
    )
    assert roots == (run_root.resolve(), claude_tmp.resolve())


def test_terminal_commands_enable_vt_engine_and_client_stays_default() -> None:
    clippy = terminal_clippy_argv()
    listed = terminal_nextest_list_argv()
    run = terminal_nextest_run_argv()
    for argv in (clippy, listed, run):
        assert argv[:2] == ["cargo", "clippy"] or argv[:3] == ["cargo", "nextest", argv[2]]
        assert "-p" in argv and "gobby-terminal" in argv
        assert "--features" in argv
        assert argv[argv.index("--features") + 1] == "vt-engine"
        assert "gobby-client" not in argv
    assert "--all-targets" in clippy
    assert "-D" in clippy and "warnings" in clippy
    assert "list" in listed
    assert "--message-format" in listed
    assert listed[listed.index("--message-format") + 1] == "json"
    assert "run" in run
    assert "--no-tests" in run and run[run.index("--no-tests") + 1] == "fail"
    client_clip = client_clippy_argv()
    client_run = client_nextest_argv()
    for argv in (client_clip, client_run):
        assert "gobby-client" in argv
        assert "gobby-terminal" not in argv
        assert "--features" not in argv
        assert "vt-engine" not in argv


def test_evaluate_gated_targets_fails_when_required_binary_missing() -> None:
    payload = _complete_gated_payload()
    del payload["rust-suites"]["gobby-terminal::embed"]
    with pytest.raises(GuardSetGError, match="embed"):
        evaluate_gated_targets(payload)


def test_evaluate_gated_targets_fails_when_required_binary_has_zero_tests() -> None:
    payload = _complete_gated_payload()
    payload["rust-suites"]["gobby-terminal::embed"]["testcases"] = {}
    with pytest.raises(GuardSetGError, match="embed"):
        evaluate_gated_targets(payload)


def test_evaluate_gated_targets_fails_when_required_binary_is_skipped() -> None:
    payload = _complete_gated_payload()
    payload["rust-suites"]["gobby-terminal::embed"] = _suite("embed", skip=True)
    with pytest.raises(GuardSetGError, match="embed"):
        evaluate_gated_targets(payload)


def test_evaluate_gated_targets_accepts_populated_required_binaries() -> None:
    assert evaluate_gated_targets(_complete_gated_payload()) == REQUIRED_GATED_TARGETS


def test_evaluate_gated_targets_accepts_json_text() -> None:
    noise = "   Compiling gobby-terminal v0.1.0\n"
    assert evaluate_gated_targets(noise + json.dumps(_complete_gated_payload())) == (
        REQUIRED_GATED_TARGETS
    )
