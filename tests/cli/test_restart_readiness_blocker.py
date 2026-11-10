"""A restart that never sees its backend ready fails within its bound and names the blocking state."""

import time
from contextlib import nullcontext
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit

BLOCKED_TARGET = "127.0.0.1:60987"
READINESS_BUDGET_SECONDS = 1.0
BOUND_SECONDS = 10.0
LOCK_HOLDER = "admin lease request"
LOCK_WAIT = f"backend supervisor waiting on the lease lock held by {LOCK_HOLDER}"


@pytest.mark.parametrize(
    ("backend", "named"),
    [
        pytest.param(
            {"state": "starting", "target": BLOCKED_TARGET},
            ("starting", BLOCKED_TARGET),
            id="state-and-target",
        ),
        pytest.param(
            {"state": "starting", "target": BLOCKED_TARGET, "wait": LOCK_WAIT},
            ("starting", BLOCKED_TARGET, LOCK_HOLDER),
            id="lock-holder",
        ),
    ],
)
def test_restart_wait_names_the_blocking_backend_state_when_readiness_times_out(
    monkeypatch: pytest.MonkeyPatch,
    backend: dict[str, str],
    named: tuple[str, ...],
) -> None:
    runtime = MagicMock(spec=CliRuntime)
    runtime.read_only_operational_config.return_value = DaemonConfig(daemon_port=61987)
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: runtime)
    monkeypatch.setattr("gobby.releases.activation.caller_is_spawned", lambda: False)
    monkeypatch.setattr("gobby.releases.activation.agent_activation_refusal", lambda _verb: None)
    monkeypatch.setattr(lifecycle, "restart_start_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(lifecycle, "clear_protected_runs", lambda *_a, **_k: True)
    monkeypatch.setattr(lifecycle, "protect_pending_handoffs", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(lifecycle, "daemon_auth_headers", lambda: {})
    monkeypatch.setattr(lifecycle, "_is_process_alive", lambda _pid: False)
    monkeypatch.setattr(lifecycle, "STARTUP_READINESS_TIMEOUT_SECONDS", READINESS_BUDGET_SECONDS)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: httpx.Response(200, json={"status": "restarting", "backend_pid": 42}),
    )
    # The front door answers readiness with the backend still starting, forever.
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_a, **_k: httpx.Response(503, json={"backend": backend}),
    )

    started = time.monotonic()
    result = CliRunner().invoke(lifecycle.restart, ["--wait"])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_SECONDS, f"the restart wait was not bounded: {elapsed:.1f}s"
    assert result.exit_code == 1, result.output
    timeout_lines = [
        line for line in result.output.splitlines() if "Startup readiness timed out" in line
    ]
    assert timeout_lines, f"no timeout line names the blocker: {result.output}"
    assert all(part in timeout_lines[0] for part in named), result.output
