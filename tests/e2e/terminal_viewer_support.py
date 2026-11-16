"""Shared native-terminal viewer helpers for isolated end-to-end tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import httpx

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameClient, FrameProtocolError
from tests._timing import wait_for_condition
from tests.e2e.conftest import DaemonInstance
from tests.native_binary_selection import select_native_binary

E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
VIEWER_COLS, VIEWER_ROWS = 80, 24
APPROVAL_PROMPT = (
    "Tool call needs your approval.\n1. Allow / 2. Cancel\nPress Enter to approve this command\n"
)


def _gterm_bin_dir() -> Path:
    selected = select_native_binary("gterm", required=True)
    assert selected is not None
    return selected.path.parent


def _wait_for_host(client: httpx.Client, daemon: DaemonInstance | None = None) -> dict[str, Any]:
    last: dict[str, Any] = {}

    def snapshot() -> dict[str, Any]:
        response = client.get("/api/health")
        if response.status_code != 200:
            last["health_status"] = response.status_code
            last["health_body"] = response.text[:500]
            return {}
        payload = response.json()
        last["health"] = payload.get("gterm_host")
        last["degraded"] = payload.get("degraded_services")
        host = payload.get("gterm_host")
        if isinstance(host, dict) and host.get("running") and host.get("host_epoch"):
            return cast(dict[str, Any], host)
        return {}

    try:
        return wait_for_condition(snapshot, timeout=20.0, interval=0.2, description="gterm host")
    except AssertionError as exc:
        logs = ""
        if daemon is not None:
            logs = (
                f"\nlogs:\n{daemon.read_logs()[-2000:]}\nerror:\n{daemon.read_error_logs()[-2000:]}"
            )
            for relative in (Path(".gobby") / "logs" / "gterm.log", Path("logs") / "gterm.log"):
                gterm_log = daemon.gobby_home / relative
                if gterm_log.is_file():
                    logs += f"\ngterm {gterm_log}:\n{gterm_log.read_text()[-2000:]}"
        raise AssertionError(f"{exc}; last={last}{logs}") from exc


def _attach_from_item(item: dict[str, Any]) -> AttachLocator:
    attach = item.get("attach")
    assert isinstance(attach, dict)
    backend = attach.get("backend")
    assert backend in {"native"}
    return AttachLocator(
        backend=backend,
        frame_host_epoch=str(attach["frame_host_epoch"]),
        host_socket=str(attach["host_socket"]),
        host_terminal_id=str(attach["host_terminal_id"]),
    )


def _frame_text(message: dict[str, Any]) -> str | None:
    if message.get("type") != "frame":
        return None
    width = int(message["width"])
    cells = message["cells"]
    rows: list[str] = []
    current: list[str] = []
    for index, cell in enumerate(cells):
        if index and index % width == 0:
            rows.append("".join(current))
            current = []
        current.append(str(cell.get("symbol") or ""))
    if current:
        rows.append("".join(current))
    return "\n".join(rows)


async def _open_viewer(
    locator: AttachLocator,
    token: str,
    *,
    cols: int,
    rows: int,
) -> FrameClient:
    assert locator.host_socket is not None
    host_dir = os.environ.get("GOBBY_E2E_HOST_SOCKET_DIR")
    frame_token = token
    if host_dir:
        from gobby.utils.local_token import read_local_api_token

        frame_token = read_local_api_token(Path(host_dir) / "bootstrap.yaml") or ""
    reader, writer = await asyncio.open_unix_connection(locator.host_socket)
    client = FrameClient(reader, writer)
    await client.handshake(locator, local_token=frame_token, cols=cols, rows=rows)
    await client.attach_terminal(locator)
    try:
        first = await asyncio.wait_for(client.read_message(), timeout=5.0)
    except (FrameProtocolError, TimeoutError) as exc:
        host_dir = os.environ.get("GOBBY_E2E_HOST_SOCKET_DIR")
        extra = ""
        if host_dir:
            log_path = Path(host_dir) / "logs" / "gterm.log"
            if log_path.is_file():
                extra = f"\ngterm log:\n{log_path.read_text()[-4000:]}"
            pid_path = Path(host_dir) / "gterm.pid"
            if pid_path.is_file():
                extra += f"\npidfile={pid_path.read_text().strip()}"
                extra += (
                    "\nps="
                    + subprocess.run(
                        ["ps", "-p", pid_path.read_text().strip(), "-o", "args="],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                )
        raise AssertionError(f"attach read failed: {exc}; locator={locator}{extra}") from exc
    if first.get("type") == "error":
        raise AssertionError(f"attach refused: {first}")
    client._queue.append(first)
    return client


async def _read_until(
    client: FrameClient,
    predicate: Any,
    *,
    timeout: float,
    description: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = list(client._queue)
    client._queue.clear()
    for message in messages:
        if predicate(message):
            return messages
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            message = await asyncio.wait_for(client.read_message(), timeout=min(0.25, remaining))
        except TimeoutError:
            continue
        messages.append(message)
        if predicate(message):
            return messages
    brief = [
        {
            "type": message.get("type"),
            "width": message.get("width"),
            "height": message.get("height"),
            "code": message.get("code"),
            "cursor_visible": (message.get("modes") or {}).get("cursor_visible"),
            "keypad_cursor": (message.get("modes") or {}).get("keypad_cursor"),
            "pane_in_mode": (message.get("modes") or {}).get("pane_in_mode"),
        }
        for message in messages[-8:]
    ]
    raise AssertionError(f"timed out waiting for {description}: {brief}")
