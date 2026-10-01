"""Violation capture in the SRT runner, driven against a stub sandbox-runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_RUNNER = Path(__file__).resolve().parents[2] / "src" / "gobby" / "agents" / "srt_runner.mjs"

# Mirrors the pinned sandbox-runtime SandboxViolationStore: a 100-entry tail,
# a monotonic total, and a synchronous notify after every add. Wrapping the
# command emits RUN_EVENTS while subscribed; LATE_EVENTS arrive after the
# runner unsubscribes, so only its final flush can record them.
_STUB_RUNTIME = """
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
  initialize: async () => {},
  getSandboxViolationStore: () => store,
  wrapWithSandboxArgv: async command => {
    for (let i = 0; i < RUN_EVENTS; i++) store.addViolation({ line: `v${i}` })
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
    assert violations.stat().st_mode & 0o777 == 0o600
