"""Failure and identity checks for the placed-runbook live harness (#23335).

These run without a daemon, SRT or gterm: real processes stand in for the host
and for the SRT runner that wraps a provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

import tests.workflows.placed_runbook_live_support as support
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    incompatible_spawn_model_provider,
)
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.providers.capabilities.seed import apply_seed
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.model_metadata import ModelMetadataStore
from gobby.terminals.host_protocol import control_socket_path, pidfile_path, read_pidfile
from tests.workflows.placed_runbook_live_support import (
    LAUNCH_LOG,
    Attempts,
    find_standin,
    fixture_host,
    host_socket_dir,
    is_standin,
    launch_markers,
    live_standins,
    seed_seat_catalog,
    start_fixture_host,
    write_standin,
)

STANDIN = "/runs/providers/codex"


def _poll[T](probe: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + 10
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "the probe never matched"
        time.sleep(0.05)
    return value


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _children() -> set[int]:
    return {child.pid for child in psutil.Process().children(recursive=True)}


def _wrapped_standin(standin: Path, runner: Path, tmpdir: Path) -> subprocess.Popen[bytes]:
    """A wrapper that carries the stand-in path in its own argv, as the SRT runner does."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; subprocess.run(sys.argv[3:])",
            str(runner),
            "--",
            str(standin),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env={**os.environ, "TMPDIR": str(tmpdir)},
        start_new_session=True,
    )


def _close(process: subprocess.Popen[bytes]) -> None:
    # End of input is how a closed terminal ends the stand-in.
    assert process.stdin is not None
    process.stdin.close()
    process.wait(timeout=10)


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/bin/sh", STANDIN], True),
        ([STANDIN], True),
        (["node", "/srt/runner.mjs", "--settings", "s.json", "--", STANDIN], False),
        (["sandbox-exec", "-p", "(version 1)", STANDIN], False),
        (["/bin/sh", "-c", f"{STANDIN} --model gpt"], False),
    ],
    ids=["interpreter", "direct", "srt-runner", "sandbox-exec", "shell-command"],
)
def test_is_standin_matches_only_the_stand_in_itself(cmdline: list[str], expected: bool) -> None:
    assert is_standin(cmdline, {STANDIN}) is expected


def test_find_standin_returns_the_provider_rather_than_its_wrapper(tmp_path: Path) -> None:
    standin = write_standin(tmp_path, "codex")
    runner = tmp_path / "runner.mjs"
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    wrapper = _wrapped_standin(standin, runner, run_tmp)
    try:
        found = _poll(lambda: find_standin(wrapper.pid, {str(standin)}, str(runner)))
        assert found.pid != wrapper.pid
        assert psutil.Process(found.pid).cmdline()[1] == str(standin)
        assert found.launches == (found.pid,)
        assert found.wrapped
        other = find_standin(wrapper.pid, {str(standin)}, str(tmp_path / "other.mjs"))
        assert other is not None and not other.wrapped
        assert live_standins({str(standin)}) == [found.pid]
    finally:
        _close(wrapper)
    assert live_standins({str(standin)}) == []


def test_launch_markers_count_a_second_launch_into_the_same_run(tmp_path: Path) -> None:
    standin = write_standin(tmp_path, "grok")
    runner = tmp_path / "runner.mjs"
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    wrappers = [_wrapped_standin(standin, runner, run_tmp)]
    try:
        original = _poll(lambda: find_standin(wrappers[0].pid, {str(standin)}, str(runner)))
        wrappers.append(_wrapped_standin(standin, runner, run_tmp))
        relaunch = _poll(lambda: find_standin(wrappers[1].pid, {str(standin)}, str(runner)))
        assert launch_markers(psutil.Process(original.pid)) == (original.pid, relaunch.pid)
        assert live_standins({str(standin)}) == sorted([original.pid, relaunch.pid])
    finally:
        for wrapper in wrappers:
            _close(wrapper)


def test_start_fixture_host_reaps_a_host_that_never_gets_ready(tmp_path: Path) -> None:
    binary = _script(tmp_path / "never-ready", "exec sleep 30")
    before = _children()
    with pytest.raises(AssertionError, match="never served its socket"):
        start_fixture_host(binary, tmp_path / "sockets", os.environ, ready_timeout=0.3)
    # Neither a running host nor an unreaped zombie is left behind.
    assert _children() == before


def test_fixture_host_is_killed_and_its_directory_removed_when_shutdown_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ShutdownFailed(Exception):
        pass

    async def refuse(socket_dir: Path) -> None:
        raise ShutdownFailed(socket_dir)

    monkeypatch.setattr(support, "_shutdown", refuse)
    with pytest.raises(ShutdownFailed), host_socket_dir() as socket_dir:
        ready = (
            f': > "{control_socket_path(socket_dir)}"\n'
            f'printf %s "$$" > "{pidfile_path(socket_dir)}"\n'
            "exec sleep 30"
        )
        binary = _script(tmp_path / "ready-host", ready)
        with fixture_host(binary, socket_dir, os.environ) as host:
            assert host.alive() and read_pidfile(socket_dir) == host.pid
    assert not host.alive()
    assert not socket_dir.exists()


def test_attempts_runs_every_step_and_raises_the_failures_together() -> None:
    record: dict[str, Any] = {}
    attempts = Attempts(record)

    def kill() -> None:
        raise ConnectionError("runner is down")

    assert attempts.run("kill", kill) is None
    assert attempts.run("close", lambda: "closed") == "closed"
    with pytest.raises(ExceptionGroup) as raised:
        attempts.check()
    assert [type(error) for error in raised.value.exceptions] == [ConnectionError]
    assert record == {"kill": {"error": "ConnectionError('runner is down')"}, "close": "closed"}


# The bundled seats: plan-writer, plan-enhancer and plan-adversary.
SEATS = [
    ("codex", "gpt-5.6-sol", "medium"),
    ("codex", "gpt-5.6-sol", "xhigh"),
    ("grok", "grok-4.7", "xhigh"),
]


def test_seeded_catalog_admits_runbook_seat_models(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    seed_seat_catalog(postgres_db, [(provider, model) for provider, model, _ in SEATS])
    # Daemon start, then a refresh whose probes the stand-ins refuse.
    apply_seed(store)
    store.record_source_failure("codex", "app-server-model-list", "stand-in refused the probe")
    store.record_source_failure("grok", "local-model-discovery", "stand-in refused the probe")
    resolver = CapabilityResolver(store, ModelMetadataStore(postgres_db))
    for provider, model, effort in SEATS:
        assert resolver.find_model(provider, model) is not None, (provider, model)
        assert (
            incompatible_spawn_model_provider(provider=provider, model=model, resolver=resolver)
            is None
        )
        resolution = resolver.resolve_reasoning(
            provider, model, effort, transport_supports_effort=True
        )
        assert resolution.effective_effort == effort, resolution


@pytest.mark.parametrize(
    ("provider", "model", "probe"),
    [
        # CodexAppServerClient.start: [codex, *global_args, "app-server", ...].
        ("codex", "gpt-5.6-sol", ["app-server"]),
        ("codex", "gpt-5.6-sol", ["--enable", "feature", "app-server", "-c", "key=value"]),
        # grok_acp_client: [grok, "agent", "--no-leader", "--always-approve", ..., "stdio"].
        ("grok", "grok-4.7", ["agent", "--no-leader", "--always-approve", "stdio"]),
    ],
)
def test_standin_refuses_capability_probes(
    tmp_path: Path, provider: str, model: str, probe: list[str]
) -> None:
    standin = write_standin(tmp_path, provider)
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    env = {**os.environ, "TMPDIR": str(run_tmp)}
    refused = subprocess.run(
        [str(standin), *probe], env=env, input="", capture_output=True, text=True, timeout=10
    )
    assert refused.returncode == 64, refused
    assert not (run_tmp / LAUNCH_LOG).exists()
    seat, _ = build_cli_command(
        cli=provider, prompt="plan", auto_approve=True, working_directory=str(tmp_path), model=model
    )
    launched = subprocess.run(
        [str(standin), *seat[1:]], env=env, input="", capture_output=True, text=True, timeout=10
    )
    assert launched.returncode == 0, launched
    ready, *composer = launched.stdout.splitlines()
    marker, pid = ready.split()
    assert marker == f"INERT-{provider}-READY"
    assert (run_tmp / LAUNCH_LOG).read_text(encoding="utf-8").split() == [pid]
    # The Codex spawn types its prompt only once the pane shows the "›" composer.
    assert composer == (["› "] if provider == "codex" else []), launched.stdout
