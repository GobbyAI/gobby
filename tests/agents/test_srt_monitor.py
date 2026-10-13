"""Shared host monitor tests with real Node/socket/process boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import pty
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO

import pytest

from gobby.agents import srt_monitor
from gobby.utils.dependency_requirements import SRT_RELEASE

pytestmark = pytest.mark.unit
_RUNNER = Path(__file__).resolve().parents[2] / "src/gobby/agents/srt_runner.mjs"
_RUNTIME = """
const listeners = new Set()
const violations = []
let transport
const store = {
  subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) },
  getTotalCount: () => violations.length,
  getViolations: () => [...violations],
}
export const SandboxRuntimeConfigSchema = { safeParse: data => ({ success: true, data }) }
export const SandboxManager = {
  isSupportedPlatform: () => true,
  initialize: async () => {
    transport = globalThis.gobbySrtLogTransport(process.env.TEST_SUFFIX)
    transport.stdout.on('data', data => {
      violations.push({ line: data.toString(), command: process.env.TEST_SUFFIX })
      listeners.forEach(listener => listener())
      console.log('recorded')
    })
  },
  getSandboxViolationStore: () => store,
  wrapWithSandboxArgv: async command => ({ argv: ['/bin/sh', '-c', command], env: {} }),
  reset: async () => transport.kill(),
}
"""


@dataclass
class MonitorHarness:
    root: Path
    node: Path
    runner: Path
    events: IO[str]
    socket_path: str

    def event(self, message: str) -> None:
        self.events.write(json.dumps({"eventMessage": message}) + "\n")
        self.events.flush()

    def finish(self) -> None:
        self.events.write("END-TEST-STREAM\n")
        self.events.flush()
        self.events.close()

    def launch(self, suffix: str, *, descendant: bool = False) -> subprocess.Popen[str]:
        settings = self.root / f"{suffix}.settings.json"
        settings.write_text("{}")
        ready = "console.log(JSON.stringify(process.env.GOBBY_SRT_MONITOR_SOCKET ?? null));"
        if descendant:
            descendant_code = (
                "require('fs').writeFileSync(process.argv[1], process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');"
                "require('net').createServer(socket => socket.end()).listen(process.argv[2], () => console.log('ready'));"
                "process.on('SIGTERM', () => {});"
            )
            ready = (
                "const child = require('child_process').spawn(process.execPath, ['-e', "
                + json.dumps(descendant_code)
                + ", process.argv[2], process.argv[3]], { stdio: ['ignore','pipe','inherit'] });"
                + "child.stdout.once('data', () => {"
                + ready
                + "});"
            )
        program = (
            "require('fs').writeFileSync(process.argv[1], process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');"
            + ready
            + "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000)"
        )
        return subprocess.Popen(
            [
                str(self.node),
                str(self.runner),
                "--settings",
                str(settings),
                "--violations",
                str(self.root / f"{suffix}.jsonl"),
                "--",
                str(self.node),
                "-e",
                program,
                str(self.root / f"{suffix}.pid"),
                str(self.root / f"{suffix}.child.pid"),
                str(self.root / f"{suffix[1:3]}.s"),
            ],
            env={**os.environ, "GOBBY_SRT_MONITOR_SOCKET": self.socket_path, "TEST_SUFFIX": suffix},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


@pytest.fixture
def monitor_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[MonitorHarness]:
    node_name = shutil.which("node")
    if node_name is None:
        pytest.skip("Node required")
    node = Path(node_name)
    # Keep the Unix socket below macOS's 104-byte sun_path limit.
    with tempfile.TemporaryDirectory(prefix="sm-", dir=os.environ.get("TMPDIR")) as temporary:
        root = Path(temporary)
        event_file = root / "events"
        event_file.write_text("")
        fake_log = root / "log.mjs"
        fake_log.write_text(
            f"#!{node}\n"
            "import { openSync, fstatSync, readSync, closeSync, appendFileSync } from 'node:fs';\n"
            "console.log('Filtering the log data using test predicate');\n"
            f"appendFileSync({json.dumps(str(root / 'starts'))}, 'start\\n');\n"
            f"const events = {json.dumps(str(event_file))};\n"
            "const fd = openSync(events, 'r'); let offset = 0; let pending = '';\n"
            "const timer = setInterval(() => {\n"
            " const size = fstatSync(fd).size; if (size > offset) {\n"
            "  const bytes = Buffer.alloc(size-offset); const count = readSync(fd, bytes, 0, bytes.length, offset);\n"
            "  offset += count; pending += bytes.subarray(0,count).toString();\n"
            " }\n"
            " let end; while ((end = pending.indexOf('\\n')) !== -1) {\n"
            "  const line = pending.slice(0, end); pending = pending.slice(end + 1);\n"
            "  if (line === 'END-TEST-STREAM') { clearInterval(timer); closeSync(fd); return; }\n"
            "  process.stdout.write(line+'\\n');\n"
            " }\n"
            "}, 5);\n"
        )
        fake_log.chmod(0o700)
        fake_ps = root / "ps.mjs"
        fake_ps.write_text("#!/bin/sh\n/bin/cat " + shlex.quote(str(root)) + "/*.pid\n")
        fake_ps.chmod(0o700)
        dist = root / "node_modules/@anthropic-ai/sandbox-runtime/dist"
        dist.mkdir(parents=True)
        (dist / "index.js").write_text(_RUNTIME)
        (root / "package.json").write_text('{"type":"module"}')
        runner = root / "runner.mjs"
        runner.write_text(
            "Object.defineProperty(process, 'platform', { value: 'darwin' });\n"
            + f"if (process.argv[2] !== '--monitor') appendFileSync({json.dumps(str(root))} + '/' + process.pid + '.pid', process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');\n"
            + _RUNNER.read_text()
            .removeprefix("#!/usr/bin/env node\n")
            .replace("spawn('/usr/bin/log', [", f"spawn({json.dumps(str(fake_log))}, [")
            .replace("execFileSync('/bin/ps',", f"execFileSync({json.dumps(str(fake_ps))},")
            .replace(
                "providerIdentity = await transport.providerStarted(child.pid)",
                f"appendFileSync({json.dumps(str(root))} + '/' + child.pid + '.pid', child.pid+' '+process.pid+' 123 '+child.pid+'-start\\n');\n"
                "providerIdentity = await transport.providerStarted(child.pid)",
            )
        )
        identity = hashlib.sha256(runner.read_bytes()).hexdigest()
        monkeypatch.setattr(
            srt_monitor, "SRT_RELEASE", replace(SRT_RELEASE, runner_sha256=identity)
        )
        monkeypatch.setattr(srt_monitor, "srt_mux_tmpdir", lambda: root)
        socket_path = srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ)
        monitor = srt_monitor._children[-1]
        try:
            with event_file.open("a") as events:
                yield MonitorHarness(root, node, runner, events, socket_path)
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)


def _subscribe(path: str, suffix: str) -> tuple[socket.socket, IO[bytes]]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(path)
    client.sendall(json.dumps({"suffix": suffix}).encode() + b"\n")
    stream = client.makefile("rb")
    assert json.loads(stream.readline())["ready"] is True
    return client, stream


def test_one_stream_routes_complete_events_and_failure_closes_all(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness

    def reuse(_index: int) -> str:
        return srt_monitor.ensure_srt_monitor(
            node=harness.node, runner=harness.runner, env=os.environ
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(reuse, range(4))) == [harness.socket_path] * 4
    assert (harness.root / "starts").read_text().splitlines() == ["start"]
    first, first_stream = _subscribe(harness.socket_path, "_first_SBX")
    second, second_stream = _subscribe(harness.socket_path, "_second_SBX")
    with first, first_stream, second, second_stream:
        first_message = "Sandbox: first deny file-read-data /first\nCMD64_x_END__first_SBX"
        second_message = "Sandbox: second deny file-write-data /second\nCMD64_y_END__second_SBX"
        harness.event(second_message)
        harness.event(first_message)
        assert json.loads(first_stream.readline())["message"] == first_message
        assert json.loads(second_stream.readline())["message"] == second_message
        # EOF makes the fake log process exit, exercising stream death rather
        # than explicitly shutting down the broker.
        harness.finish()
        assert first_stream.readline() == b""
        assert second_stream.readline() == b""


def test_subscription_loss_terminates_provider_and_records_its_events(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    children = {
        suffix: harness.launch(suffix, descendant=True) for suffix in ("_first_SBX", "_second_SBX")
    }
    try:
        for child in children.values():
            assert child.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=15):
                    child.terminate()
                    stdout, stderr = child.communicate(timeout=5)
                    pytest.fail(
                        f"provider readiness failed: {child.returncode}, {stdout}, {stderr}"
                    )
                assert json.loads(child.stdout.readline()) is None
        for suffix in children:
            harness.event(f"Sandbox: provider deny file-read-data /private {suffix}")
        for child in children.values():
            assert child.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=15)
                assert child.stdout.readline().strip() == "recorded"
        # The broker sends records before EOF on the same connection.
        harness.finish()
        for suffix, child in children.items():
            stdout, stderr = child.communicate(timeout=8)
            assert child.returncode == 1, (stdout, stderr)
            assert "shared violation monitor disconnected" in stderr
            records = [
                json.loads(line)
                for line in (harness.root / f"{suffix}.jsonl").read_text().splitlines()
            ]
            assert records == [
                {
                    "line": f"Sandbox: provider deny file-read-data /private {suffix}",
                    "command": suffix,
                }
            ]
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as descendant:
                with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
                    descendant.connect(str(harness.root / f"{suffix[1:3]}.s"))
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_live_monitor_identity_mismatch_never_replaces_subscribers(
    monitor_harness: MonitorHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = monitor_harness
    client, stream = _subscribe(harness.socket_path, "_existing_SBX")
    with client, stream:
        monkeypatch.setattr(srt_monitor, "SRT_RELEASE", replace(SRT_RELEASE, runner_sha256="wrong"))
        with pytest.raises(RuntimeError, match="identity mismatch"):
            srt_monitor._connect(Path(harness.socket_path))
        harness.event("Sandbox: existing deny file-read-data /secret _existing_SBX")
        assert json.loads(stream.readline())["message"].endswith("_existing_SBX")
        assert (harness.root / "starts").read_text().splitlines() == ["start"]


def test_malformed_and_foreign_events_are_retained_without_disconnecting_subscribers(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    client, stream = _subscribe(harness.socket_path, "_healthy_SBX")
    with client, stream:
        harness.events.write("malformed-record\n")
        harness.events.flush()
        harness.event("foreign X_SBX")
        harness.event("Sandbox: healthy deny file-read-data /secret _healthy_SBX")
        assert json.loads(stream.readline())["message"].endswith("_healthy_SBX")
        journal = Path(harness.socket_path + ".jsonl")
        records = [json.loads(line)["record"] for line in journal.read_text().splitlines()]
        assert records[0] == "malformed-record"
        assert len(records) == 1
        assert journal.stat().st_mode & 0o777 == 0o600


def test_runner_update_drains_old_broker_and_new_idle_broker_exits(
    monitor_harness: MonitorHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = monitor_harness
    old_client, old_stream = _subscribe(harness.socket_path, "_old_SBX")
    with old_client, old_stream:
        updated = harness.root / "updated.mjs"
        updated.write_text(
            harness.runner.read_text().replace("setTimeout(stop, 30000)", "setTimeout(stop, 50)")
        )
        identity = hashlib.sha256(updated.read_bytes()).hexdigest()
        monkeypatch.setattr(
            srt_monitor, "SRT_RELEASE", replace(SRT_RELEASE, runner_sha256=identity)
        )
        new_path = srt_monitor.ensure_srt_monitor(node=harness.node, runner=updated, env=os.environ)
        new_monitor = srt_monitor._children[-1]
        try:
            assert new_path != harness.socket_path
            new_monitor.wait(timeout=5)
            assert new_monitor.returncode == 0
            assert not Path(new_path).exists()
            harness.event("Sandbox: old deny file-read-data /old _old_SBX")
            assert json.loads(old_stream.readline())["message"].endswith("_old_SBX")
            assert (harness.root / "starts").read_text().splitlines() == ["start", "start"]
        finally:
            if new_monitor.poll() is None:
                new_monitor.terminate()
                new_monitor.wait(timeout=5)


def test_slow_subscriber_isolated_from_healthy_subscriber(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    slow, slow_stream = _subscribe(harness.socket_path, "_slow_SBX")
    healthy, healthy_stream = _subscribe(harness.socket_path, "_healthy_SBX")
    with slow, slow_stream, healthy, healthy_stream:
        for _ in range(4000):
            harness.events.write(json.dumps({"eventMessage": "x" * 2048 + " _slow_SBX"}) + "\n")
        harness.events.flush()
        harness.event("Sandbox: healthy deny file-read-data /healthy _healthy_SBX")
        assert json.loads(healthy_stream.readline())["message"].endswith("_healthy_SBX")
        try:
            while slow_stream.readline():
                pass
        except ConnectionResetError:
            pass
        failure = Path(harness.socket_path + "._slow_SBX.failed.jsonl")
        assert 0 < failure.stat().st_size <= 2 * 1024 * 1024
        saved = [json.loads(line) for line in failure.read_text().splitlines()]
        assert all(record["message"].endswith("_slow_SBX") for record in saved)
        harness.event("Sandbox: healthy deny file-write-data /still-live _healthy_SBX")
        assert "/still-live" in json.loads(healthy_stream.readline())["message"]


def test_diagnostics_rotate_and_unsubscribed_events_do_not_grow_storage(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    client, stream = _subscribe(harness.socket_path, "_healthy_SBX")
    with client, stream:
        for _ in range(700):
            harness.events.write("malformed-" + "x" * 4096 + "\n")
            harness.events.write(json.dumps({"eventMessage": "foreign _unused_SBX"}) + "\n")
        harness.events.flush()
        harness.event("Sandbox: healthy deny file-read-data /alive _healthy_SBX")
        assert "/alive" in json.loads(stream.readline())["message"]
        journal = Path(harness.socket_path + ".jsonl")
        backup = Path(harness.socket_path + ".jsonl.1")
        assert 0 < journal.stat().st_size <= 1024 * 1024
        assert 0 < backup.stat().st_size <= 1024 * 1024
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        assert all(record["record"].startswith("malformed-") for record in records)


def test_acknowledged_records_stay_only_in_execution_log(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    child = harness.launch("_acked_SBX")
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
            harness.event("Sandbox: provider deny file-read-data /acked _acked_SBX")
            assert selector.select(timeout=15)
            assert child.stdout.readline().strip() == "recorded"
        child.send_signal(signal.SIGINT)
        child.communicate(timeout=15)
        assert child.returncode == -signal.SIGINT
        assert not Path(harness.socket_path + "._acked_SBX.failed.jsonl").exists()
        assert Path(harness.socket_path + ".jsonl").stat().st_size == 0
        records = [
            json.loads(line)
            for line in (harness.root / "_acked_SBX.jsonl").read_text().splitlines()
        ]
        assert records[0]["line"].endswith("_acked_SBX")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("scenario", ["reused", "unrelated"])
def test_provider_identity_guards_refuse_reused_or_unowned_pid(
    monitor_harness: MonitorHarness,
    scenario: str,
) -> None:
    harness = monitor_harness
    victim = subprocess.Popen([str(harness.node), "-e", "setInterval(() => {}, 1000)"])
    script = harness.root / "identity-probe.mjs"
    marker = harness.root / "identity-victim.pid"
    script.write_text(
        "import { writeFileSync } from 'node:fs';\n"
        f"import {{ captureProviderIdentity, stopProviderTree }} from {json.dumps(harness.runner.as_uri())};\n"
        f"const pid = {victim.pid}; const path = {json.dumps(str(marker))};\n"
        f"const scenario = {json.dumps(scenario)};\n"
        "const parent = scenario === 'reused' ? process.pid : 0;\n"
        "writeFileSync(path, `${pid} ${parent} 123 before\\n`);\n"
        "try {\n"
        " const identity = captureProviderIdentity(pid, process.pid);\n"
        " writeFileSync(path, `${pid} ${parent} 123 reused-start\\n`);\n"
        " stopProviderTree(pid, identity); throw new Error('guard did not refuse');\n"
        "} catch (error) {\n"
        " const expected = scenario === 'reused' ? 'identity changed' : 'not a descendant';\n"
        " if (!error.message.includes(expected)) throw error;\n"
        " console.log('refused');\n"
        "}\n"
    )
    try:
        result = subprocess.run(
            [str(harness.node), str(script)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "refused"
        assert victim.poll() is None
    finally:
        victim.terminate()
        victim.wait(timeout=5)


def test_missing_monitor_prevents_provider_launch(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    settings = harness.root / "settings.json"
    settings.write_text("{}")
    env = dict(os.environ)
    env.pop("GOBBY_SRT_MONITOR_SOCKET", None)
    result = subprocess.run(
        [
            str(harness.node),
            str(harness.runner),
            "--settings",
            str(settings),
            "--violations",
            str(harness.root / "missing.jsonl"),
            "--",
            "/bin/echo",
            "provider-ran",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert "missing shared SRT violation monitor" in result.stderr
    assert "provider-ran" not in result.stdout


def test_terminal_keeps_controlling_tty_and_fails_closed(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    settings = harness.root / "tty.settings.json"
    settings.write_text("{}")
    pid, master = pty.fork()
    if pid == 0:
        os.execve(
            str(harness.node),
            [
                str(harness.node),
                str(harness.runner),
                "--settings",
                str(settings),
                "--violations",
                str(harness.root / "tty.jsonl"),
                "--",
                str(harness.node),
                "-e",
                "require('fs').writeFileSync(process.argv[1], process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');"
                "require('fs').closeSync(require('fs').openSync('/dev/tty', 'r'));"
                "console.log('tty-ready'); setInterval(() => {}, 1000)",
                str(harness.root / "terminal.pid"),
            ],
            {
                **os.environ,
                "GOBBY_SRT_MONITOR_SOCKET": harness.socket_path,
                "TEST_SUFFIX": "_terminal_SBX",
            },
        )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            output = os.read(master, 4096)
            assert b"tty-ready" in output, output
            harness.finish()
            while True:
                assert selector.select(timeout=8), "terminal survived monitor loss"
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # macOS/Linux can report PTY EOF as EIO.
                if not chunk:
                    break
            waited, status = os.waitpid(pid, 0)
            assert waited == pid
            assert os.WIFEXITED(status)
            assert os.WEXITSTATUS(status) == 1
    finally:
        os.close(master)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
