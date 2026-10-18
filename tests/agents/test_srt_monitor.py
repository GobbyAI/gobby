"""Shared host monitor tests with real Node/socket/process boundaries."""

from __future__ import annotations

import base64
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
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO

import psutil
import pytest

from gobby.agents import srt_monitor
from gobby.utils.dependency_requirements import SRT_RELEASE
from tests.srt_fixture_helpers import write_srt_parser_fixture

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

    def launch(
        self, suffix: str, *, descendant: bool = False, orphan: bool = False
    ) -> subprocess.Popen[str]:
        settings = self.root / f"{suffix}.settings.json"
        settings.write_text("{}")
        ready = "console.log(JSON.stringify(process.env.GOBBY_SRT_MONITOR_SOCKET ?? null));"
        if descendant:
            descendant_code = (
                "require('fs').writeFileSync(process.argv[1], process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');"
                "require('net').createServer(socket => socket.end()).listen(process.argv[2], () => console.log('ready'));"
                "process.on('SIGTERM', () => {});"
            )
            if orphan:
                # Record the actual OS parent only after the intermediary exits.
                descendant_code = (
                    "const originalParent = process.ppid;"
                    "const timer = setInterval(() => {"
                    " if (process.ppid !== originalParent || process.ppid === 1) {"
                    "  clearInterval(timer);" + descendant_code + " } }, 5);"
                )
                descendant_code = (
                    "const child = require('child_process').spawn(process.execPath, ['-e', "
                    + json.dumps(descendant_code)
                    + ", ...process.argv.slice(1)], { detached: true, stdio: ['ignore','inherit','inherit'] });"
                    "child.unref();"
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
            env={
                **os.environ,
                "GOBBY_SRT_MONITOR_SOCKET": self.socket_path,
                "GOBBY_SRT_RUN_KEY": suffix.strip("_"),
                "TEST_SUFFIX": suffix,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


@pytest.fixture
def monitor_harness(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[MonitorHarness]:
    node_name = shutil.which("node")
    if node_name is None:
        pytest.skip("Node required")
    node = Path(node_name)
    # Keep the Unix socket below macOS's 104-byte sun_path limit.
    with tempfile.TemporaryDirectory(prefix="sm-", dir=os.environ.get("TMPDIR")) as temporary:
        root = Path(temporary)
        if getattr(request, "param", None) == "symlink-temp":
            alias = root / "alias"
            alias.symlink_to(root.resolve(), target_is_directory=True)
            root = alias
        # Node canonicalizes import.meta.url. Its direct-entry guard must see
        # the same path in argv, including macOS /var -> /private/var TMPDIRs.
        root = root.resolve()
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
        fake_ps.write_text(
            "#!/bin/sh\n"
            + f"if [ -f {shlex.quote(str(root / 'ps-delay'))} ]; then /bin/sleep $(/bin/cat {shlex.quote(str(root / 'ps-delay'))}); fi\n"
            + "/bin/cat "
            + shlex.quote(str(root))
            + "/*.pid\n"
        )
        fake_ps.chmod(0o700)
        dist = root / "node_modules/@anthropic-ai/sandbox-runtime/dist"
        dist.mkdir(parents=True)
        runtime = _RUNTIME
        if getattr(request, "param", None) == "lingering-reset":
            runtime = runtime.replace(
                "reset: async () => transport.kill()",
                "reset: async () => { setInterval(() => {}, 1000) }",
            )
        elif getattr(request, "param", None) == "hung-reset":
            runtime = runtime.replace(
                "reset: async () => transport.kill()",
                "reset: async () => new Promise(() => {})",
            )
        (dist / "index.js").write_text(runtime)
        (root / "package.json").write_text('{"type":"module"}')
        runner = root / "runner.mjs"
        runner.write_text(
            "Object.defineProperty(process, 'platform', { value: 'darwin' });\n"
            + f"appendFileSync({json.dumps(str(root))} + '/' + process.pid + '.pid', process.pid+' '+process.ppid+' 123 '+process.pid+'-start\\n');\n"
            + _RUNNER.read_text()
            .removeprefix("#!/usr/bin/env node\n")
            .replace("spawn('/usr/bin/log', [", f"spawn({json.dumps(str(fake_log))}, [")
            .replace("execFileAsync('/bin/ps',", f"execFileAsync({json.dumps(str(fake_ps))},")
            .replace("function sharedTransport(", "export function sharedTransport(")
            .replace(
                "const monitorRegistrationTimeoutMs = 30000",
                f"const monitorRegistrationTimeoutMs = {request.param if isinstance(getattr(request, 'param', None), int) else 30000}",
            )
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
        monkeypatch.setattr(srt_monitor, "get_gobby_home", lambda: root)
        socket_path = srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ)
        monitor = srt_monitor._children[-1]
        try:
            with event_file.open("a") as events:
                yield MonitorHarness(root, node, runner, events, socket_path)
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)


def _subscribe(path: str, suffix: str) -> tuple[socket.socket, IO[bytes]]:
    (Path(path).parent / "observer.pid").write_text(
        f"{os.getpid()} {os.getppid()} 123 {os.getpid()}-start\n"
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15)
    client.connect(path)
    client.sendall(
        json.dumps(
            {"suffix": suffix, "runKey": suffix.strip("_"), "runnerPid": os.getpid()}
        ).encode()
        + b"\n"
    )
    stream = client.makefile("rb")
    assert json.loads(stream.readline())["ready"] is True
    return client, stream


def _wait_path(path: Path, *, absent: bool = False, minimum_bytes: int = 1) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if absent and not path.exists():
                return
            if not absent and path.stat().st_size >= minimum_bytes:
                return
        except FileNotFoundError:
            if absent:
                return
        time.sleep(0.01)
    pytest.fail(f"backlog path did not reach expected state: {path}")


@pytest.mark.parametrize("monitor_harness", [None, "symlink-temp"], indirect=True)
def test_one_stream_routes_complete_events_and_failure_closes_all(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    broker = srt_monitor._children[-1]
    assert os.getsid(broker.pid) == broker.pid
    assert os.getsid(broker.pid) != os.getsid(0)

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


def test_isolated_restart_reaper_preserves_monitor_and_subscription(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    program = r"""
import asyncio, hashlib, json, os, socket, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
from gobby.agents import srt_monitor
from gobby.runner_lifecycle_processes import _reap_remaining_child_processes
root, node, runner = map(Path, sys.argv[1:])
temporary = tempfile.TemporaryDirectory(prefix='sr-', dir=root.parent)
directory = Path(temporary.name)
srt_monitor.srt_mux_tmpdir = lambda: directory
srt_monitor.get_gobby_home = lambda: root
srt_monitor.SRT_RELEASE = replace(srt_monitor.SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest())
path = srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ)
broker = srt_monitor._children[-1]
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(10)
client.connect(path)
(root / 'restart-observer.pid').write_text(f'{os.getpid()} {os.getppid()} 123 {os.getpid()}-start\n')
client.sendall(json.dumps({'suffix':'_restart_SBX', 'runKey':'restart-run', 'runnerPid':os.getpid()}).encode()+b'\n')
stream = client.makefile('rb')
noise = subprocess.Popen([str(node), '-e', 'setInterval(() => {}, 1000)'])
try:
    assert json.loads(stream.readline())['ready']
    before = (root / 'starts').read_text()
    asyncio.run(_reap_remaining_child_processes(preserve_agents=True, preserve_srt_monitor=True))
    noise.wait(timeout=5)
    assert noise.poll() is not None
    assert broker.poll() is None
    assert srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ) == path
    assert (root / 'starts').read_text() == before
    message = 'Sandbox: restart deny file-read-data /restart\nCMD64_x_END__restart_SBX'
    with (root / 'events').open('a') as events:
        events.write(json.dumps({'eventMessage':message})+'\n')
    assert json.loads(stream.readline())['message'] == message
    asyncio.run(_reap_remaining_child_processes(preserve_agents=True, preserve_srt_monitor=False))
    broker.wait(timeout=5)
    assert broker.poll() is not None
    assert stream.readline() == b''
    print('restart preserved monitor, stream and subscription; stop cleaned up')
finally:
    client.close()
    stream.close()
    for child in (noise, broker):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    temporary.cleanup()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(harness.root), str(harness.node), str(harness.runner)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "restart preserved monitor, stream and subscription; stop cleaned up" in result.stdout


def test_restart_reap_on_the_production_path_keeps_a_broker_started_by_a_worker_thread(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    program = r"""
import asyncio, hashlib, json, logging, os, socket, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from gobby.agents import srt_monitor
from gobby.runner_lifecycle_processes import (
    _preserved_agent_terminal_pids,
    _reap_remaining_child_processes,
)
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(message)s')
root, node, runner = map(Path, sys.argv[1:])
temporary = tempfile.TemporaryDirectory(prefix='sr-', dir=root.parent)
directory = Path(temporary.name)
srt_monitor.srt_mux_tmpdir = lambda: directory
srt_monitor.get_gobby_home = lambda: root
srt_monitor.SRT_RELEASE = replace(srt_monitor.SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest())


async def main() -> None:
    # The spawn path starts the broker from a worker thread, not the loop thread.
    path = await asyncio.to_thread(
        srt_monitor.ensure_srt_monitor,
        node=node,
        runner=runner,
        env=os.environ,
        run_key='prod-run',
    )
    broker = srt_monitor._children[-1]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10)
    noise = subprocess.Popen([str(node), '-e', 'setInterval(() => {}, 1000)'])
    stream = None
    try:
        identities = await asyncio.to_thread(srt_monitor.live_monitor_identities)
        assert broker.pid in identities, identities
        client.connect(path)
        # The fixture's fake ps reads root/*.pid, so register this runner's identity first.
        (root / 'runner-observer.pid').write_text(f'{os.getpid()} {os.getppid()} 123 {os.getpid()}-start\n')
        subscribe = {'suffix': '_prod_SBX', 'runKey': 'prod-run', 'runnerPid': os.getpid()}
        client.sendall(json.dumps(subscribe).encode() + b'\n')
        stream = client.makefile('rb')
        assert json.loads(stream.readline())['ready']
        runner_state = SimpleNamespace(terminal_host_manager=None, agent_runner=None)
        preserved = await _preserved_agent_terminal_pids(runner_state)
        await _reap_remaining_child_processes(
            preserve_agents=True,
            preserved_agent_pids=preserved,
            preserve_srt_monitor=True,
        )
        noise.wait(timeout=5)
        assert noise.poll() is not None
        assert broker.poll() is None
        message = 'Sandbox: prod deny file-read-data /prod\nCMD64_x_END__prod_SBX'
        with (root / 'events').open('a') as events:
            events.write(json.dumps({'eventMessage': message}) + '\n')
        assert json.loads(stream.readline())['message'] == message
        print('production restart reap preserved the broker and its subscription')
    finally:
        if stream is not None:
            stream.close()
        client.close()
        for child in (noise, broker):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        temporary.cleanup()


asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(harness.root), str(harness.node), str(harness.runner)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "production restart reap preserved the broker and its subscription" in result.stdout
    assert "during restart" in result.stderr


# Starts a broker from a child process, as a long-lived worker would, so the reaping
# process never registers it in its own _children.
_BROKER_WORKER = r"""
import hashlib, json, os, sys
from dataclasses import replace
from pathlib import Path
from gobby.agents import srt_monitor
root, node, runner, directory = map(Path, sys.argv[1:])
srt_monitor.srt_mux_tmpdir = lambda: directory
srt_monitor.get_gobby_home = lambda: root
srt_monitor.SRT_RELEASE = replace(srt_monitor.SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest())
path = srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ, run_key='prod-run')
print(json.dumps({'path': path, 'pid': srt_monitor._children[-1].pid}), flush=True)
sys.stdin.read()
"""


def test_restart_reap_keeps_a_broker_started_by_a_worker_process(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    program = r"""
import asyncio, hashlib, json, os, signal, stat, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
import psutil
from gobby.agents import srt_monitor
from gobby.runner_lifecycle_processes import _reap_remaining_child_processes
root, node, runner = map(Path, sys.argv[1:4])
worker_program = sys.argv[4]
temporary = tempfile.TemporaryDirectory(prefix='sw-', dir=root.parent)
directory = Path(temporary.name)
srt_monitor.srt_mux_tmpdir = lambda: directory
srt_monitor.get_gobby_home = lambda: root
srt_monitor.SRT_RELEASE = replace(srt_monitor.SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest())


async def main() -> None:
    worker = subprocess.Popen(
        [sys.executable, '-c', worker_program, str(root), str(node), str(runner), str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    started = None
    try:
        started = json.loads(await asyncio.to_thread(worker.stdout.readline))
        # The daemon reaches the live broker through ensure_srt_monitor, which adopts it.
        await asyncio.to_thread(
            srt_monitor.ensure_srt_monitor,
            node=node,
            runner=runner,
            env=os.environ,
            run_key='prod-run',
        )
        identities = await asyncio.to_thread(srt_monitor.live_monitor_identities)
        assert started['pid'] in identities, identities
        record = Path(started['path'] + '.owner.json')
        assert stat.S_IMODE(record.stat().st_mode) == 0o600
        await _reap_remaining_child_processes(
            preserve_agents=True,
            preserved_agent_pids=set(),
            preserve_srt_monitor=True,
        )
        try:
            survivor = psutil.Process(started['pid'])
            alive = survivor.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            alive = False
        assert alive, 'restart reap killed the worker-started broker'
        print('restart reap kept the worker-started broker')
    finally:
        worker.stdin.close()
        worker.wait(timeout=10)
        if started is not None:
            try:
                os.kill(started['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        temporary.cleanup()


asyncio.run(main())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(harness.root),
            str(harness.node),
            str(harness.runner),
            _BROKER_WORKER,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "restart reap kept the worker-started broker" in result.stdout


@pytest.mark.parametrize("drift", ["exe", "argv", "started", "listener", "symlink"])
def test_adopted_broker_refuses_each_drifted_pin(
    monitor_harness: MonitorHarness,
    drift: str,
) -> None:
    harness = monitor_harness
    program = r"""
import asyncio, hashlib, json, os, signal, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
from gobby.agents import srt_monitor
root, node, runner = map(Path, sys.argv[1:4])
worker_program, drift = sys.argv[4], sys.argv[5]
temporary = tempfile.TemporaryDirectory(prefix='sd-', dir=root.parent)
directory = Path(temporary.name)
srt_monitor.srt_mux_tmpdir = lambda: directory
srt_monitor.get_gobby_home = lambda: root
srt_monitor.SRT_RELEASE = replace(srt_monitor.SRT_RELEASE, runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest())


def ensure() -> str:
    return srt_monitor.ensure_srt_monitor(node=node, runner=runner, env=os.environ, run_key='prod-run')


async def main() -> None:
    worker = subprocess.Popen(
        [sys.executable, '-c', worker_program, str(root), str(node), str(runner), str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    noise = subprocess.Popen([str(node), '-e', 'setInterval(() => {}, 1000)'])
    started = None
    try:
        started = json.loads(await asyncio.to_thread(worker.stdout.readline))
        record = Path(started['path'] + '.owner.json')
        await asyncio.to_thread(ensure)
        identities = await asyncio.to_thread(srt_monitor.live_monitor_identities)
        assert started['pid'] in identities, identities
        if drift == 'symlink':
            copy = record.with_name(record.name + '.copy')
            copy.write_bytes(record.read_bytes())
            record.unlink()
            record.symlink_to(copy)
        else:
            data = json.loads(record.read_text())
            if drift == 'exe':
                data['node'] = '/bin/sh'
            elif drift == 'argv':
                data['argv'][-1] = str(directory / 'elsewhere')
            elif drift == 'started':
                data['started'] = repr(float(data['started']) + 1.0)
            elif drift == 'listener':
                data['pid'] = noise.pid
            record.write_text(json.dumps(data))
        await asyncio.to_thread(ensure)
        identities = await asyncio.to_thread(srt_monitor.live_monitor_identities)
        assert started['pid'] not in identities, identities
        print('drifted pin refused: ' + drift)
    finally:
        worker.stdin.close()
        worker.wait(timeout=10)
        if started is not None:
            try:
                os.kill(started['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        noise.kill()
        noise.wait(timeout=5)
        temporary.cleanup()


asyncio.run(main())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(harness.root),
            str(harness.node),
            str(harness.runner),
            _BROKER_WORKER,
            drift,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"drifted pin refused: {drift}" in result.stdout


def test_registration_survives_slow_identity_work(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    (harness.root / "ps-delay").write_text("6")
    provider = harness.launch("_slow_SBX")
    try:
        assert provider.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(provider.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=25), "slow registration never released provider"
            assert provider.stdout.readline().strip() == "null"
        (harness.root / "ps-delay").unlink()
        message = "Sandbox: slow deny file-read-data /slow\nCMD64_x_END__slow_SBX"
        harness.event(message)
        with selectors.DefaultSelector() as selector:
            selector.register(provider.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10)
            assert provider.stdout.readline().strip() == "recorded"
        assert json.loads((harness.root / "_slow_SBX.jsonl").read_text())["line"] == message
        harness.finish()
        assert provider.wait(timeout=10) != 0
    finally:
        if provider.poll() is None:
            provider.kill()
        provider.wait(timeout=5)


@pytest.mark.parametrize("monitor_harness", [250], indirect=True)
def test_registration_deadline_fails_closed_before_provider_launch(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    (harness.root / "ps-delay").write_text("1")
    runner = harness.launch("_deadline_SBX")
    stdout, stderr = runner.communicate(timeout=5)
    assert runner.returncode == 1, (stdout, stderr)
    assert any(
        error in stderr
        for error in ("shared monitor readiness timed out", "shared violation monitor disconnected")
    )
    assert not (harness.root / "_deadline_SBX.pid").exists()
    diagnostic = Path(harness.socket_path + ".jsonl")
    _wait_path(diagnostic)
    records = [json.loads(line) for line in diagnostic.read_text().splitlines()]
    assert any(record["kind"] == "registration" for record in records)


def test_registration_failure_records_bounded_diagnostic(monitor_harness: MonitorHarness) -> None:
    harness = monitor_harness
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    with client:
        client.connect(harness.socket_path)
        client.sendall(b'{"suffix":"_invalid_SBX","runKey":"invalid-run","runnerPid":1}\n')
        assert client.recv(1) == b""
    diagnostic = Path(harness.socket_path + ".jsonl")
    _wait_path(diagnostic)
    record = json.loads(diagnostic.read_text().splitlines()[0])
    assert record["kind"] == "registration"
    assert record["record"] == "invalid-run"
    assert record["error"] == "invalid runner PID"


@pytest.mark.parametrize("monitor_harness", ["lingering-reset", "hung-reset"], indirect=True)
def test_monitor_loss_exits_runner_after_cli_child_exits(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    runner = harness.launch("_exit_SBX")
    try:
        assert runner.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(runner.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert runner.stdout.readline().strip() == "null"
        harness.finish()
        stdout, stderr = runner.communicate(timeout=15)
        assert runner.returncode == 1, (stdout, stderr)
        assert "shared violation monitor disconnected" in stderr
        assert not psutil.pid_exists(runner.pid)
    finally:
        if runner.poll() is None:
            runner.kill()
        runner.wait(timeout=5)


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


def test_lagging_subscriber_replays_in_order_without_blocking_healthy_subscriber(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    slow, slow_stream = _subscribe(harness.socket_path, "_slow_SBX")
    healthy, healthy_stream = _subscribe(harness.socket_path, "_healthy_SBX")
    with slow, slow_stream, healthy, healthy_stream:
        messages = [f"{index}:" + "x" * 2048 + " _slow_SBX" for index in range(600)]
        for message in messages:
            harness.events.write(json.dumps({"eventMessage": message}) + "\n")
        harness.events.flush()
        harness.event("Sandbox: healthy deny file-read-data /healthy _healthy_SBX")
        assert json.loads(healthy_stream.readline())["message"].endswith("_healthy_SBX")
        last_id = 0
        for message in messages:
            record = json.loads(slow_stream.readline())
            assert record["message"] == message
            assert record["id"] > last_id
            last_id = record["id"]
            slow.sendall(json.dumps({"ack": record["id"]}).encode() + b"\n")
        _wait_path(harness.root / "slow_SBX.srt-backlog.jsonl.active", absent=True)
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
        assert not (harness.root / "acked_SBX.srt-backlog.jsonl").exists()
        assert not (harness.root / "acked_SBX.srt-backlog.jsonl.active").exists()
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
        " const identity = await captureProviderIdentity(pid, process.pid);\n"
        " writeFileSync(path, `${pid} ${parent} 123 reused-start\\n`);\n"
        " await stopProviderTree(pid, identity); throw new Error('guard did not refuse');\n"
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
                "GOBBY_SRT_RUN_KEY": "terminal",
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


def test_pinned_upstream_parser_records_transport_attribution_and_applies_ignores(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    parser = write_srt_parser_fixture(harness.root)
    store = parser.with_name("sandbox-violation-store.js")
    script = harness.root / "parser-probe.mjs"
    script.write_text(
        f"import {{ sharedTransport }} from {json.dumps(harness.runner.as_uri())};\n"
        f"import {{ startMacOSSandboxLogMonitor }} from {json.dumps(parser.as_uri())};\n"
        f"import {{ SandboxViolationStore }} from {json.dumps(store.as_uri())};\n"
        "let transport; const store = new SandboxViolationStore();\n"
        "globalThis.gobbySrtLogTransport = suffix => {\n"
        " console.log(JSON.stringify({suffix}));\n"
        f" transport = sharedTransport({json.dumps(harness.socket_path)}, suffix, error => {{\n"
        "  console.error(error.message); process.exitCode = 1;\n"
        " }, 'parser-proof'); return transport;\n"
        "};\n"
        "startMacOSSandboxLogMonitor(violation => {\n"
        " store.addViolation(violation); console.log(JSON.stringify(store.getViolations()));\n"
        "}, { '*': ['/ignored'] }, key => key === 'owned-key' ? 'rg owned-command' : undefined);\n"
        "await transport.ready; console.log('ready');\n"
    )
    child = subprocess.Popen(
        [str(harness.node), str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            suffix = json.loads(child.stdout.readline())["suffix"]
            assert child.stdout.readline().strip() == "ready"
            encoded = base64.b64encode(b"owned-key").decode()
            harness.event(
                f"Sandbox: node(123) deny(1) file-read-data /ignored\nCMD64_{encoded}_END_{suffix}"
            )
            harness.event(
                f"Sandbox: node(123) deny(1) file-read-data /owned\nCMD64_{encoded}_END_{suffix}"
            )
            assert selector.select(timeout=15)
            records = json.loads(child.stdout.readline())
            assert len(records) == 1
            assert records[0]["line"] == "node(123) deny(1) file-read-data /owned"
            assert records[0]["command"] == "rg owned-command"
            assert records[0]["encodedCommand"] == encoded
        harness.finish()
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 1, (stdout, stderr)
        assert "shared violation monitor disconnected" in stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_monitor_loss_closes_sink_but_reparented_worker_survives(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    child = harness.launch("_orphan_SBX", descendant=True, orphan=True)
    worker_pid: int | None = None
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
        fields = (harness.root / "_orphan_SBX.child.pid").read_text().split()
        worker_pid = int(fields[0])
        assert int(fields[1]) == 1, "worker must actually be reparented before failure"
        harness.finish()
        # The surviving worker inherits stderr; communicate would wait for its
        # pipe EOF and obscure the specific cleanup failure below.
        child.wait(timeout=8)
        assert child.returncode == 1
        # Approved scope: monitor loss kills attached descendants and closes
        # the shared sink. Already-reparented workers remain sandboxed and live.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sink:
            with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
                sink.connect(harness.socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as worker:
            worker.connect(str(harness.root / "or.s"))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
