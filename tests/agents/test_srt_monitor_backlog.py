"""Shared-monitor durable delivery and registration boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shlex
import signal
import socket
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from gobby.agents import srt_monitor
from gobby.utils.dependency_requirements import SRT_RELEASE
from tests.agents.test_srt_monitor import MonitorHarness, _subscribe, _wait_path, monitor_harness

__all__ = ["monitor_harness"]
pytestmark = pytest.mark.unit


def test_stopped_runner_replays_large_backlog_without_stopping_provider(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    child = harness.launch("_paused_SBX", descendant=True)
    backlog = Path(harness.socket_path).parent / "paused_SBX.srt-backlog.jsonl.active"
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
            child.send_signal(signal.SIGSTOP)
            messages = [
                f"Sandbox: provider deny file-read-data /{index}-{'x' * (192 * 1024)} _paused_SBX"
                for index in range(8)
            ]
            for message in messages:
                harness.event(message)
            _wait_path(backlog, minimum_bytes=1024 * 1024 + 1)
            assert child.poll() is None
            with socket.socket(socket.AF_UNIX) as application:
                application.connect(str(harness.root / "pa.s"))
            child.send_signal(signal.SIGCONT)
            for _message in messages:
                assert selector.select(timeout=15)
                assert child.stdout.readline().strip() == "recorded"
            _wait_path(backlog, absent=True)
            records = [
                json.loads(line)
                for line in (harness.root / "_paused_SBX.jsonl").read_text().splitlines()
            ]
            assert [record["line"] for record in records] == messages
            assert child.poll() is None
        harness.finish()
        child.communicate(timeout=15)
        assert child.returncode == 1
        with socket.socket(socket.AF_UNIX) as application:
            with pytest.raises(OSError):
                application.connect(str(harness.root / "pa.s"))
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGCONT)
            harness.finish()
            child.wait(timeout=15)


@pytest.mark.parametrize("run_key", ["../escape", "", "unicode-é", "x" * 129])
def test_invalid_run_key_refuses_registration(
    monitor_harness: MonitorHarness, run_key: str
) -> None:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(15)
        client.connect(monitor_harness.socket_path)
        client.sendall(json.dumps({"suffix": "_invalid_SBX", "runKey": run_key}).encode() + b"\n")
        assert client.recv(1024) == b""
    assert not list(monitor_harness.root.glob("*.srt-backlog.jsonl*"))


def test_duplicate_run_key_refuses_second_subscription_and_keeps_first(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    first, records = _subscribe(harness.socket_path, "_duplicate_SBX")
    try:
        with socket.socket(socket.AF_UNIX) as second:
            second.settimeout(15)
            second.connect(harness.socket_path)
            second.sendall(b'{"suffix":"_other_SBX","runKey":"duplicate_SBX"}\n')
            assert second.recv(1024) == b""
        message = "Sandbox: provider deny file-read-data /owned _duplicate_SBX"
        harness.event(message)
        record = json.loads(records.readline())
        assert record["message"] == message
        first.sendall(json.dumps({"ack": record["id"]}).encode() + b"\n")
    finally:
        records.close()
        first.close()


def test_killed_runner_retains_unacknowledged_backlog_and_stops_provider(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    child = harness.launch("_killed_SBX", descendant=True)
    directory = Path(harness.socket_path).parent
    active = directory / "killed_SBX.srt-backlog.jsonl.active"
    retained = directory / "killed_SBX.srt-backlog.jsonl"
    message = "Sandbox: provider deny file-read-data /pending _killed_SBX"
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
        child.send_signal(signal.SIGSTOP)
        initial_bytes = active.stat().st_size
        harness.event(message)
        _wait_path(active, minimum_bytes=initial_bytes + 1)
        child.kill()
        child.communicate(timeout=15)
        assert child.returncode == -signal.SIGKILL
        _wait_path(retained)
        records = [json.loads(line) for line in retained.read_text().splitlines()]
        assert records[0]["monitorBacklog"]["runKey"] == "killed_SBX"
        assert [record["message"] for record in records[1:]] == [message]
        with socket.socket(socket.AF_UNIX) as application:
            with pytest.raises(OSError):
                application.connect(str(harness.root / "ki.s"))
        assert not active.exists()
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGCONT)
            harness.finish()
            child.wait(timeout=15)


def test_disk_bound_retains_trigger_and_keeps_healthy_subscription(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    slow, slow_records = _subscribe(harness.socket_path, "_bounded_SBX")
    healthy, healthy_records = _subscribe(harness.socket_path, "_healthy_SBX")
    try:
        payload = "x" * (1024 * 1024)
        for index in range(34):
            harness.event(f"Sandbox: provider deny file-read-data /{index}-{payload} _bounded_SBX")
        retained = Path(harness.socket_path).parent / "bounded_SBX.srt-backlog.jsonl"
        _wait_path(retained, minimum_bytes=32 * 1024 * 1024)
        records = [json.loads(line) for line in retained.read_text().splitlines()]
        assert records[0]["monitorBacklog"]["runKey"] == "bounded_SBX"
        assert len(records[1:]) == 32
        assert all(f"/{index}-" in record["message"] for index, record in enumerate(records[1:]))
        message = "Sandbox: provider deny file-read-data /healthy _healthy_SBX"
        harness.event(message)
        delivered = json.loads(healthy_records.readline())
        assert delivered["message"] == message
        healthy.sendall(json.dumps({"ack": delivered["id"]}).encode() + b"\n")
    finally:
        slow_records.close()
        slow.close()
        healthy_records.close()
        healthy.close()


def test_slow_process_snapshot_leaves_probe_responsive(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    marker = harness.root / "snapshot-started"
    release = harness.root / "snapshot-release"
    (harness.root / "observer.pid").write_text(f"{os.getpid()} {os.getppid()} 123 observer-start\n")
    (harness.root / "ps.mjs").write_text(
        "#!/bin/sh\n"
        + f"/usr/bin/touch {shlex.quote(str(marker))}\n"
        + f"while [ ! -e {shlex.quote(str(release))} ]; do /bin/sleep 0.05; done\n"
        + f"/bin/cat {shlex.quote(str(harness.root))}/*.pid\n"
    )
    with socket.socket(socket.AF_UNIX) as slow, socket.socket(socket.AF_UNIX) as probe:
        slow.settimeout(15)
        slow.connect(harness.socket_path)
        slow.sendall(
            json.dumps(
                {"suffix": "_slowps_SBX", "runKey": "slowps", "runnerPid": os.getpid()}
            ).encode()
            + b"\n"
        )
        _wait_path(marker, minimum_bytes=0)
        probe.settimeout(3)
        probe.connect(harness.socket_path)
        probe.sendall(b'{"suffix":"_gobbyprobe_SBX"}\n')
        assert json.loads(probe.recv(4096))["ready"] is True
        release.touch()
        assert json.loads(slow.recv(4096))["ready"] is True


@pytest.mark.parametrize("kind", ["published", "dead-active", "live-active"])
def test_relaunch_retains_prior_evidence_and_refuses_only_a_live_owner(
    monitor_harness: MonitorHarness, kind: str
) -> None:
    harness = monitor_harness
    key = "existing_SBX"
    extension = "" if kind == "published" else ".active"
    prior = Path(harness.socket_path).parent / f"{key}.srt-backlog.jsonl{extension}"
    content = (
        json.dumps(
            {
                "monitorBacklog": {
                    "runKey": key,
                    "runnerPid": 99999999,
                    "runnerStarted": "dead",
                    "brokerPid": os.getpid(),
                    "brokerStarted": "observer-start" if kind == "live-active" else "dead",
                }
            }
        )
        + '\n{"id":7,"message":"earlier"}\n'
    )
    prior.write_text(content)
    prior.chmod(0o600)
    (harness.root / "observer.pid").write_text(f"{os.getpid()} {os.getppid()} 123 observer-start\n")
    if kind == "live-active":
        with pytest.raises(RuntimeError, match="owned by a live SRT monitor"):
            srt_monitor.ensure_srt_monitor(
                node=harness.node, runner=harness.runner, env=os.environ, run_key=key
            )
        assert prior.read_text() == content
        return
    assert (
        srt_monitor.ensure_srt_monitor(
            node=harness.node, runner=harness.runner, env=os.environ, run_key=key
        )
        == harness.socket_path
    )
    retained = harness.root / f"logs/sandbox-violations/{key}.monitor-backlog.jsonl"
    assert retained.read_text() == content
    assert not prior.exists()
    client, records = _subscribe(harness.socket_path, "_existing_SBX")
    with client, records:
        message = "Sandbox: provider deny file-read-data /relaunch _existing_SBX"
        harness.event(message)
        assert json.loads(records.readline())["message"] == message


@pytest.mark.parametrize("broker_started", [None, "observer-start", "reused-start"])
def test_new_monitor_recovers_only_backlogs_without_a_live_owner(
    monitor_harness: MonitorHarness,
    monkeypatch: pytest.MonkeyPatch,
    broker_started: str | None,
) -> None:
    harness = monitor_harness
    directory = Path(harness.socket_path).parent
    source = directory / "dead-run.srt-backlog.jsonl.active"
    content = (
        json.dumps(
            {
                "monitorBacklog": {
                    "runKey": "dead-run",
                    "runnerPid": 99999999,
                    "runnerStarted": "dead",
                    "brokerPid": os.getpid(),
                    "brokerStarted": broker_started,
                }
            }
        )
        + '\n{"id":23,"message":"deny"}\n'
    )
    source.write_text(content)
    source.chmod(0o600)
    (harness.root / "observer.pid").write_text(f"{os.getpid()} {os.getppid()} 123 observer-start\n")
    next_runner = harness.root / "next-runner.mjs"
    next_runner.write_text(harness.runner.read_text() + "\n// next monitor identity\n")
    identity = hashlib.sha256(next_runner.read_bytes()).hexdigest()
    monkeypatch.setattr(srt_monitor, "SRT_RELEASE", replace(SRT_RELEASE, runner_sha256=identity))
    next_socket = srt_monitor.ensure_srt_monitor(
        node=harness.node, runner=next_runner, env=os.environ
    )
    next_monitor = srt_monitor._children[-1]
    try:
        retained = harness.root / "logs/sandbox-violations/dead-run.monitor-backlog.jsonl"
        if broker_started == "observer-start":
            assert source.read_text() == content
            assert not retained.exists()
        else:
            assert retained.read_text() == content
            assert retained.stat().st_mode & 0o777 == 0o600
            assert not source.exists()
        assert next_socket != harness.socket_path
    finally:
        next_monitor.terminate()
        next_monitor.wait(timeout=15)


def test_malformed_startup_backlogs_are_quarantined_and_keys_can_relaunch(
    monitor_harness: MonitorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = monitor_harness
    directory = Path(harness.socket_path).parent
    cases = [("empty_SBX", ".active", ""), ("torn_SBX", "", '{"monitorBacklog":')]
    for key, extension, content in cases:
        backlog_path = directory / f"{key}.srt-backlog.jsonl{extension}"
        backlog_path.write_text(content)
        backlog_path.chmod(0o600)
    unsafe = directory / "unsafe_SBX.srt-backlog.jsonl"
    target = harness.root / "outside-evidence"
    target.write_text("do not follow")
    unsafe.symlink_to(target)
    mismatch = directory / "mismatch_SBX.srt-backlog.jsonl"
    mismatch.write_text('{"monitorBacklog":{"runKey":"different"}}\n')
    fifo = directory / "fifo_SBX.srt-backlog.jsonl"
    os.mkfifo(fifo, 0o600)
    runner = harness.root / "quarantine-runner.mjs"
    runner.write_text(harness.runner.read_text() + "\n// quarantine startup identity\n")
    monkeypatch.setattr(
        srt_monitor,
        "SRT_RELEASE",
        replace(SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest()),
    )
    socket_path = srt_monitor.ensure_srt_monitor(node=harness.node, runner=runner, env=os.environ)
    monitor = srt_monitor._children[-1]
    try:
        diagnostic = (directory / "violations-monitor.log").read_text()
        for key, extension, content in cases:
            original = directory / f"{key}.srt-backlog.jsonl{extension}"
            preserved = list(directory.glob(original.name + ".quarantine-*"))
            assert len(preserved) == 1
            assert preserved[0].read_text() == content
            assert not original.exists()
            assert diagnostic.count(f"quarantined {original.name} at") == 1
        assert len(list(directory.glob(unsafe.name + ".quarantine-*"))) == 1
        assert target.read_text() == "do not follow"
        assert not unsafe.is_symlink()
        assert len(list(directory.glob(mismatch.name + ".quarantine-*"))) == 1
        assert not mismatch.exists()
        assert diagnostic.count(f"quarantined {unsafe.name} at") == 1
        assert diagnostic.count(f"quarantined {mismatch.name} at") == 1
        preserved_fifo = list(directory.glob(fifo.name + ".quarantine-*"))
        assert len(preserved_fifo) == 1
        assert stat.S_ISFIFO(preserved_fifo[0].stat().st_mode)
        assert not fifo.exists()
        assert diagnostic.count(f"quarantined {fifo.name} at") == 1
        for key in ("empty_SBX", "torn_SBX", "unsafe_SBX", "mismatch_SBX", "fifo_SBX"):
            client, records = _subscribe(socket_path, f"_{key}")
            with client, records:
                message = f"Sandbox: provider deny file-read-data /relaunch _{key}"
                harness.event(message)
                assert json.loads(records.readline())["message"] == message
    finally:
        monitor.terminate()
        monitor.wait(timeout=15)


def test_quarantine_retention_keeps_newest_32_entries_without_following_symlinks(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    directory = Path(harness.socket_path).parent
    target = harness.root / "outside-quarantine"
    target.write_text("preserve target")
    entries = []
    for index in range(1, 41):
        entry = directory / (
            f"old.srt-backlog.jsonl.quarantine-00000000-0000-0000-0000-{index:012x}"
        )
        if index == 1:
            entry.symlink_to(target)
        else:
            entry.write_text(f"evidence {index}")
        os.utime(entry, (index, index), follow_symlinks=False)
        entries.append(entry)
    companion = Path(str(entries[-1]) + ".compact")
    companion.write_text("compaction evidence")
    os.utime(companion, (40, 40))
    unrelated = directory / "old.srt-backlog.jsonl.quarantine-user"
    unrelated.write_text("unrelated")
    malformed = directory / "new_SBX.srt-backlog.jsonl"
    malformed.write_text("")
    assert (
        srt_monitor.ensure_srt_monitor(
            node=harness.node, runner=harness.runner, env=os.environ, run_key="new_SBX"
        )
        == harness.socket_path
    )
    assert all(not entry.exists() and not entry.is_symlink() for entry in entries[:10])
    assert all(
        entry.read_text() == f"evidence {index}" for index, entry in enumerate(entries[10:], 11)
    )
    assert companion.read_text() == "compaction evidence"
    preserved = list(directory.glob(malformed.name + ".quarantine-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == ""
    assert len(list(directory.glob("*.quarantine-*"))) == 33  # 32 managed plus unrelated.
    assert target.read_text() == "preserve target"
    assert unrelated.read_text() == "unrelated"
    client, records = _subscribe(harness.socket_path, "_new_SBX")
    with client, records:
        message = "Sandbox: provider deny file-read-data /after-prune _new_SBX"
        harness.event(message)
        assert json.loads(records.readline())["message"] == message


def test_done_queued_behind_ack_finishes_before_close_cleanup(
    monitor_harness: MonitorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = monitor_harness
    entered = harness.root / "ack-entered"
    release = harness.root / "ack-release"
    runner = harness.root / "done-runner.mjs"
    delay = (
        f"appendFileSync({json.dumps(str(entered))}, 'entered');\n"
        "await new Promise(resolve => { const timer = setInterval(() => {"
        f" if (existsSync({json.dumps(str(release))})) {{ clearInterval(timer); resolve(); }}"
        " }, 5); });\nawait state.backlog.ack(request.ack)"
    )
    runner.write_text(
        "import { existsSync } from 'node:fs';\n"
        + harness.runner.read_text()
        .removeprefix("#!/usr/bin/env node\n")
        .replace("await state.backlog.ack(request.ack)", delay)
    )
    monkeypatch.setattr(
        srt_monitor,
        "SRT_RELEASE",
        replace(SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest()),
    )
    path = srt_monitor.ensure_srt_monitor(node=harness.node, runner=runner, env=os.environ)
    monitor = srt_monitor._children[-1]
    client, records = _subscribe(path, "_cleandone_SBX")
    active = Path(path).parent / "cleandone_SBX.srt-backlog.jsonl.active"
    try:
        harness.event("Sandbox: provider deny file-read-data /pending _cleandone_SBX")
        record = json.loads(records.readline())
        client.sendall(json.dumps({"ack": record["id"]}).encode() + b'\n{"done":true}\n')
        client.shutdown(socket.SHUT_WR)
        _wait_path(entered)
        # Live ownership protects the queue until the final ACK/done settles.
        with pytest.raises(RuntimeError, match="owned by a live SRT monitor"):
            srt_monitor.ensure_srt_monitor(
                node=harness.node, runner=runner, env=os.environ, run_key="cleandone_SBX"
            )
        assert active.exists()
        release.touch()
        _wait_path(active, absent=True)
        assert not active.with_suffix("").exists()
        assert monitor.poll() is None
        assert (
            srt_monitor.ensure_srt_monitor(
                node=harness.node, runner=runner, env=os.environ, run_key="cleandone_SBX"
            )
            == path
        )
        relaunched, replay = _subscribe(path, "_cleandone_SBX")
        with relaunched, replay:
            message = "Sandbox: provider deny file-read-data /retry _cleandone_SBX"
            harness.event(message)
            assert json.loads(replay.readline())["message"] == message
    finally:
        release.touch()
        records.close()
        client.close()
        monitor.terminate()
        monitor.wait(timeout=15)


def test_provider_gate_closing_before_go_uses_normal_failure_path(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    source = harness.runner.read_text()
    identity = hashlib.sha256(harness.runner.read_bytes()).hexdigest()
    identity_line = next(
        line for line in source.splitlines() if line.startswith("const runnerIdentity =")
    )
    # Pin this test copy to the already-running broker, then kill the gated
    # child before release. The fake process table preserves its captured row.
    harness.runner.write_text(
        source.replace(identity_line, f"const runnerIdentity = {json.dumps(identity)}").replace(
            "providerIdentity = await transport.providerStarted(child.pid)",
            "child.kill('SIGKILL'); await exited;\n"
            "providerIdentity = await transport.providerStarted(child.pid)",
        )
    )
    child = harness.launch("_gate_SBX")
    try:
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 1
        assert stdout == ""
        assert any(message in stderr for message in ("EPIPE", "destroyed", "write after end"))
        assert "Unhandled 'error' event" not in stderr
    finally:
        if child.poll() is None:
            harness.finish()
            child.wait(timeout=15)


def test_repeated_idle_rearms_do_not_stop_new_subscriber(
    monitor_harness: MonitorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = monitor_harness
    runner = harness.root / "idle-runner.mjs"
    armed = harness.root / "timer-armed"
    fire = harness.root / "timer-fire"
    fired = harness.root / "timer-fired"
    clock = (
        "import { writeFileSync, existsSync } from 'node:fs';\n"
        "const timers = new Map(); let timerId = 0;\n"
        f"const armedPath = {json.dumps(str(armed))};\n"
        f"const firePath = {json.dumps(str(fire))};\n"
        f"const firedPath = {json.dumps(str(fired))};\n"
        "function testArmIdle(callback) { const id = ++timerId; timers.set(id, callback);"
        " appendFileSync(armedPath, 'armed\\n'); return id; }\n"
        "function testClearIdle(id) { timers.delete(id); }\n"
        "setInterval(() => { if (existsSync(firePath) && !existsSync(firedPath)) {"
        " for (const callback of timers.values()) callback();"
        " writeFileSync(firedPath, 'fired'); } }, 5).unref();\n"
    )
    runner.write_text(
        clock
        + harness.runner.read_text()
        .removeprefix("#!/usr/bin/env node\n")
        .replace("setTimeout(stop, 30000)", "testArmIdle(stop)")
        .replace("clearTimeout(idleTimer)", "testClearIdle(idleTimer)")
        # Exercise repeated idle notifications before the next connection.
        .replace("      armIdle()\n    })", "      armIdle(); armIdle()\n    })")
    )
    monkeypatch.setattr(
        srt_monitor,
        "SRT_RELEASE",
        replace(SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest()),
    )
    path = srt_monitor.ensure_srt_monitor(node=harness.node, runner=runner, env=os.environ)
    monitor = srt_monitor._children[-1]
    subscriber = None
    records = None
    try:
        for suffix in ("_idleone_SBX", "_idletwo_SBX"):
            client, stream = _subscribe(path, suffix)
            previous_bytes = armed.stat().st_size
            stream.close()
            client.close()
            _wait_path(armed, minimum_bytes=previous_bytes + 1)
        subscriber, records = _subscribe(path, "_idlelive_SBX")
        fire.touch()
        _wait_path(fired)
        assert monitor.poll() is None
        message = "Sandbox: provider deny file-read-data /alive _idlelive_SBX"
        harness.event(message)
        record = json.loads(records.readline())
        assert record["message"] == message
        subscriber.sendall(json.dumps({"ack": record["id"]}).encode() + b"\n")
    finally:
        if records is not None:
            records.close()
        if subscriber is not None:
            subscriber.close()
        monitor.terminate()
        monitor.wait(timeout=15)
