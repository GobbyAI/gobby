"""Violation capture in the SRT runner, driven against a stub sandbox-runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_RUNNER = Path(__file__).resolve().parents[2] / "src" / "gobby" / "agents" / "srt_runner.mjs"
# The runner's MAX_COMMAND_CHARS and MAX_RECORDED_VIOLATIONS.
_COMMAND_CEILING = 1024
_RECORDED_CAP = 1000

# Mirrors the pinned sandbox-runtime SandboxViolationStore: a 100-entry tail,
# a monotonic total, and a synchronous notify after every add. Wrapping the
# command emits RUN_EVENTS while subscribed; LATE_EVENTS arrive after the
# runner unsubscribes, so only its final flush can record them. Each violation
# carries a COMMAND_CHARS-long command, as real entries repeat the provider's
# full command line.
_STUB_RUNTIME = """
const RUN_EVENTS = Number(process.env.STUB_RUN_EVENTS)
const LATE_EVENTS = Number(process.env.STUB_LATE_EVENTS)
const COMMAND = 'c'.repeat(Number(process.env.STUB_COMMAND_CHARS))
const violation = index => ({ line: `v${index}`, command: COMMAND, encodedCommand: 'Yw==' })

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
      for (let i = 0; i < LATE_EVENTS; i++) this.addViolation(violation(RUN_EVENTS + i))
    }
  }
  notify() { const snapshot = this.getViolations(); this.listeners.forEach(l => l(snapshot)) }
}

const store = new Store()

export const SandboxRuntimeConfigSchema = { safeParse: data => ({ success: true, data }) }

export const SandboxManager = {
  isSupportedPlatform: () => true,
  initialize: async () => {},
  getSandboxViolationStore: () => store,
  wrapWithSandboxArgv: async command => {
    for (let i = 0; i < RUN_EVENTS; i++) store.addViolation(violation(i))
    return { argv: ['/bin/sh', '-c', command], env: {} }
  },
  reset: async () => {},
}
"""


@pytest.fixture
def stub_runner(tmp_path: Path) -> Path:
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the SRT runner")
    dist = tmp_path / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.js").write_text(_STUB_RUNTIME, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    shutil.copyfile(_RUNNER, runner)
    return runner


def _run_runner(
    stub_runner: Path, *, run_events: int, late_events: int, command_chars: int
) -> tuple[Path, list[dict[str, Any]]]:
    """Run the runner over a command that exits 3; return the violation file and its entries."""
    root = stub_runner.parent
    settings = root / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    violations = root / "violations.jsonl"
    violations.write_text("", encoding="utf-8")
    marker = root / "ran"
    node = shutil.which("node")
    assert node is not None
    env = {
        **os.environ,
        "STUB_RUN_EVENTS": str(run_events),
        "STUB_LATE_EVENTS": str(late_events),
        "STUB_COMMAND_CHARS": str(command_chars),
    }

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
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 3, result.stderr
    assert marker.read_text(encoding="utf-8") == "ran"
    assert violations.stat().st_mode & 0o777 == 0o600
    entries = [json.loads(line) for line in violations.read_text(encoding="utf-8").splitlines()]
    return violations, entries


def test_runner_records_violations_after_the_ring_wraps(stub_runner: Path) -> None:
    _, entries = _run_runner(stub_runner, run_events=150, late_events=5, command_chars=64)

    assert [entry["line"] for entry in entries] == [f"v{i}" for i in range(155)]
    assert all(entry["command"] == "c" * 64 for entry in entries)
    assert not any("commandTruncated" in entry or "totalCount" in entry for entry in entries)


def test_runner_truncates_an_oversized_command(stub_runner: Path) -> None:
    _, entries = _run_runner(stub_runner, run_events=1, late_events=0, command_chars=200_000)

    assert entries == [
        {
            "line": "v0",
            "command": "c" * _COMMAND_CEILING,
            "encodedCommand": "Yw==",
            "commandLength": 200_000,
            "commandTruncated": True,
        }
    ]


def test_runner_bounds_a_denial_burst_and_keeps_the_latest(stub_runner: Path) -> None:
    violations, entries = _run_runner(
        stub_runner, run_events=5000, late_events=5, command_chars=60_000
    )

    assert len(entries) == _RECORDED_CAP + 1
    assert [entry["line"] for entry in entries[:-1]] == [f"v{i}" for i in range(_RECORDED_CAP)]
    summary = entries[-1]
    assert summary["line"] == "v5004"
    assert summary["totalCount"] == 5005
    assert summary["omittedCount"] == 5005 - _RECORDED_CAP
    assert summary["commandTruncated"] is True
    assert violations.stat().st_size < (_RECORDED_CAP + 1) * (_COMMAND_CEILING + 200)
