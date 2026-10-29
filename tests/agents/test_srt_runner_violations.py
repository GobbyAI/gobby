"""Violation capture in the SRT runner, driven against a stub sandbox-runtime."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_RUNNER = Path(__file__).resolve().parents[2] / "src" / "gobby" / "agents" / "srt_runner.mjs"

# Mirrors the pinned sandbox-runtime SandboxViolationStore: a 100-entry tail,
# a monotonic total, and a synchronous notify after every add. Wrapping the
# command emits RUN_EVENTS while subscribed; LATE_EVENTS arrive after the
# runner unsubscribes, so only its final flush can record them.
_STUB_RUNTIME = """
import { getPriority } from 'node:os'
const RUN_EVENTS = 150
const LATE_EVENTS = 5

class Store {
  constructor() { this.violations = []; this.totalCount = 0; this.maxSize = 100; this.listeners = new Set() }
  addViolation(violation) {
    this.violations.push(violation)
    this.totalCount++
    if (this.violations.length > this.maxSize) this.violations = this.violations.slice(-this.maxSize)
    this.notify()
  }
  getViolations(limit) { return limit === undefined ? [...this.violations] : this.violations.slice(-limit) }
  getCount() { return this.violations.length }
  getTotalCount() { return this.totalCount }
  clear() { this.violations = []; this.notify() }
  subscribe(listener) {
    this.listeners.add(listener)
    listener(this.getViolations())
    return () => {
      this.listeners.delete(listener)
      for (let i = 0; i < LATE_EVENTS; i++) this.addViolation({ line: `v${RUN_EVENTS + i}` })
    }
  }
  notify() { const snapshot = this.getViolations(); this.listeners.forEach(l => l(snapshot)) }
}

const store = new Store()

export const SandboxRuntimeConfigSchema = { safeParse: data => ({ success: true, data }) }

export const SandboxManager = {
  isSupportedPlatform: () => true,
  initialize: async () => {
    if (getPriority(0) !== 19) throw new Error('SRT initialized before host priority was set')
    for (const record of JSON.parse(process.env.GOBBY_TEST_PROXY_FAILURES ?? '[]')) {
      globalThis.gobbySrtProxyFailure?.(record)
    }
  },
  getSandboxViolationStore: () => store,
  wrapWithSandboxArgv: async command => {
    for (let i = 0; i < RUN_EVENTS; i++) store.addViolation({ line: `v${i}` })
    for (const line of JSON.parse(process.env.GOBBY_TEST_VIOLATIONS ?? '[]')) {
      store.addViolation({ line })
    }
    return { argv: ['/bin/sh', '-c', command], env: {} }
  },
  reset: async () => {},
}
"""


@pytest.fixture
def stub_runner(tmp_path: Path) -> Path:
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the SRT runner")
    try:
        os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0))
    except OSError as exc:
        pytest.skip(f"host setpriority is unavailable in this sandbox: {exc}")
    dist = tmp_path / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.js").write_text(_STUB_RUNTIME, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    # Ring-buffer and priority tests exercise Linux's unchanged monitor path.
    runner.write_text(
        "Object.defineProperty(process, 'platform', { value: 'linux' });\n"
        + _RUNNER.read_text().removeprefix("#!/usr/bin/env node\n"),
        encoding="utf-8",
    )
    return runner


def test_runner_records_violations_after_the_ring_wraps(stub_runner: Path) -> None:
    root = stub_runner.parent
    settings = root / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    violations = root / "violations.jsonl"
    violations.write_text("", encoding="utf-8")
    marker = root / "ran"
    node = shutil.which("node")
    assert node is not None

    result = subprocess.run(
        [
            node,
            str(stub_runner),
            "--settings",
            str(settings),
            "--violations",
            str(violations),
            "--",
            "/bin/sh",
            "-c",
            f"printf ran > {marker}; exit 3",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 3, result.stderr
    assert marker.read_text(encoding="utf-8") == "ran"
    lines = [json.loads(line)["line"] for line in violations.read_text().splitlines()]
    assert lines == [f"v{i}" for i in range(155)]


def test_runner_records_reported_network_targets(stub_runner: Path) -> None:
    root = stub_runner.parent
    settings = root / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    violations = root / "violations.jsonl"
    violations.write_text("", encoding="utf-8")
    targets = ["api.example.test:443", "/private/var/run/mDNSResponder", ""]
    proxy_failure = {
        "source": "srt-proxy",
        "operation": "CONNECT",
        "host": "api.example.test",
        "port": 443,
        "error_code": "ECONNREFUSED",
    }
    env = {
        **os.environ,
        "GOBBY_TEST_PROXY_FAILURES": json.dumps([proxy_failure]),
        "GOBBY_TEST_VIOLATIONS": json.dumps(
            [f"Sandbox: claude(123) deny(1) network-outbound {target}" for target in targets]
        ),
    }
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [
            node,
            str(stub_runner),
            "--settings",
            str(settings),
            "--violations",
            str(violations),
            "--",
            "/usr/bin/true",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in violations.read_text().splitlines()]
    network = [record for record in records if "network-outbound" in record.get("line", "")]
    assert [record for record in records if record.get("source") == "srt-proxy"] == [proxy_failure]
    assert [record.get("target") for record in network] == [*targets[:2], "unknown"]
    assert all(record.get("operation") == "network-outbound" for record in network)
    assert violations.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("custom_git, unresolved", [(False, False), (True, False), (False, True)])
def test_darwin_git_path_resolves_only_the_system_shim(
    stub_runner: Path, custom_git: bool, unresolved: bool
) -> None:
    root = stub_runner.parent
    developer_bin = root / "Developer" / "usr" / "bin"
    custom_bin = root / "custom-bin"
    for directory in (developer_bin, custom_bin):
        directory.mkdir(parents=True)
        git = directory / "git"
        git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        git.chmod(0o700)
    env = {"PATH": "/usr/bin:/bin", "MARKER": "preserved"}
    if custom_git:
        env["PATH"] = f"{custom_bin}:{env['PATH']}"
    resolved_git = "/usr/bin/git" if unresolved else str(developer_bin / "git")
    source = stub_runner.read_text()
    source = source.replace(
        "async function darwinGitEnvironment(", "export async function darwinGitEnvironment("
    )
    source = source.replace(
        "const execFileAsync = promisify(execFile)",
        "const execFileAsync = async (file, args) => {"
        "if (file !== '/usr/bin/xcrun' || args.join(' ') !== '--find git') throw Error('wrong lookup');"
        + ("throw Error('custom git must keep precedence');" if custom_git else "")
        + f"return {{ stdout: {json.dumps(resolved_git + chr(10))} }}; }}",
    )
    stub_runner.write_text(source)
    node = shutil.which("node")
    assert node is not None
    script = (
        f"const {{ darwinGitEnvironment }} = await import({json.dumps(stub_runner.as_uri())});"
        f"console.log(JSON.stringify(await darwinGitEnvironment({json.dumps(env)})));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if unresolved:
        assert result.returncode != 0
        assert "xcrun did not resolve a non-shim git executable" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        expected_path = env["PATH"] if custom_git else f"{developer_bin}:/usr/bin:/bin"
        assert json.loads(result.stdout) == {**env, "PATH": expected_path}


def test_runner_child_inherits_host_niceness(stub_runner: Path) -> None:
    root = stub_runner.parent
    settings = root / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    violations = root / "violations.jsonl"
    violations.write_text("", encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [
            node,
            str(stub_runner),
            "--settings",
            str(settings),
            "--violations",
            str(violations),
            "--",
            "/bin/sh",
            "-c",
            "exec "
            + shlex.join(
                [sys.executable, "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"]
            ),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == 19
