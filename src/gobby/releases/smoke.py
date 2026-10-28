"""Fixed smokes a release batch runs after it activates the daemon."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SmokeResult:
    name: str
    ok: bool
    detail: str


class DaemonProbe(Protocol):
    """Daemon reads for the smokes. Status 0 means the daemon did not answer."""

    def get(self, path: str) -> tuple[int, object]: ...

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, object]
    ) -> tuple[int, object]: ...


def run_smokes(
    probe: DaemonProbe,
    *,
    schema_pin: Callable[[], str | None],
    deadline: float,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[SmokeResult]:
    """Run every fixed smoke that reads the daemon and return one result per smoke.

    ``schema_pin`` returns why the installed schema pin differs from the workspace's,
    or None when they match. Health is polled until ``deadline`` on ``monotonic``.
    """
    health, report = _await_health(probe, deadline, poll, sleep, monotonic)
    runtime = report.get("hook_runtime")
    compatible = isinstance(runtime, dict) and runtime.get("compatible") is not False
    mismatch = schema_pin()
    tools_status, tools = probe.get("/api/mcp/gobby-tasks/tools")
    listed = isinstance(tools, dict) and bool(tools.get("tools"))
    tasks_status, tasks = probe.call_tool("gobby-tasks", "list_tasks", {"limit": 1})
    read = isinstance(tasks, dict) and tasks.get("success") is True
    return [
        health,
        SmokeResult("hook_runtime", compatible, f"hook runtime {runtime!r}"),
        SmokeResult("schema_pin", mismatch is None, mismatch or "installed pin matches"),
        SmokeResult("mcp_tools", tools_status == 200 and listed, f"HTTP {tools_status}"),
        SmokeResult("task_list", tasks_status == 200 and read, f"HTTP {tasks_status}"),
    ]


def reconnect_result(captured: frozenset[str], fenced: frozenset[str]) -> SmokeResult:
    """Fail when a run captured before activation was fenced by reconciliation after it."""
    if fenced:
        detail = f"reconciliation-fenced after recovery: {', '.join(sorted(fenced))}"
    else:
        detail = f"{len(captured)} captured runs unfenced"
    return SmokeResult("reconnect", not fenced, detail)


def _await_health(
    probe: DaemonProbe,
    deadline: float,
    poll: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[SmokeResult, dict[str, object]]:
    """Poll health until it is ok with restart recovery finished, or the deadline passes."""
    while True:
        status, body = probe.get("/api/health")
        report = body if isinstance(body, dict) else {}
        if status == 200 and report.get("status") == "ok":
            if report.get("restart_recovery_pending") is False:
                return SmokeResult("health", True, "ok"), report
            detail = "restart recovery is still pending"
        elif status == 200:
            detail = f"status {report.get('status')!r}"
        else:
            detail = f"HTTP {status}" if status else "the daemon did not answer"
        if monotonic() >= deadline:
            return SmokeResult("health", False, detail), report
        sleep(poll)
