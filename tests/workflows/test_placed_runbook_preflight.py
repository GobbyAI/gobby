"""Exercise the live module's capability gate without launching seats or daemons."""

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.slow


@pytest.mark.parametrize("capability", ["available", "refused", "missing", "timeout", "error"])
def test_live_module_requires_nested_seatbelt_once_per_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    checkout = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [checkout, os.getenv("PYTHONPATH")]))
    )
    pytester.makeconftest(
        f"""
import subprocess
from types import SimpleNamespace

import tests.workflows.test_placed_runbook_live as live
from tests.workflows.test_placed_runbook_live import require_nested_seatbelt

capability = {capability!r}
calls = []

def probe(command, **kwargs):
    calls.append(command)
    assert command == ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
    assert kwargs == dict(capture_output=True, text=True, check=False, timeout=5)
    if capability == "timeout":
        raise subprocess.TimeoutExpired(command, 5)
    if capability == "error":
        raise OSError("sandbox execution denied")
    return subprocess.CompletedProcess(
        command, 0 if capability == "available" else 71, "",
        "sandbox_apply: Operation not permitted",
    )

live.subprocess = SimpleNamespace(run=probe, TimeoutExpired=subprocess.TimeoutExpired)
live.shutil = SimpleNamespace(
    which=lambda name: None if capability == "missing" else "/usr/bin/sandbox-exec"
)

def pytest_sessionfinish(session, exitstatus):
    print("CAPABILITY_PROBE_CALLS=" + str(len(calls)))
"""
    )
    pytester.makepyfile(
        f"""
import pytest

@pytest.mark.parametrize("case", [1, 2])
def test_live_body(case):
    assert {capability!r} == "available", "unavailable capability reached live setup"
"""
    )

    result = pytester.runpytest_subprocess("-q", "-rs", "--timeout=120", "--timeout-method=thread")

    if capability == "available":
        result.assert_outcomes(passed=2)
    else:
        result.assert_outcomes(skipped=2)
        result.stdout.fnmatch_lines(["*nested Seatbelt*"])
    expected_calls = 0 if capability == "missing" else 1
    result.stdout.fnmatch_lines([f"*CAPABILITY_PROBE_CALLS={expected_calls}*"])
