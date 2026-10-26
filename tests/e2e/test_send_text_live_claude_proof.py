"""Live proof: a 2,600-character send_text into a native Claude Code pane arrives whole.

Runs only with GOBBY_LIVE_CLAUDE_PROOF=1 and a `claude` binary on PATH. The pane gets an
isolated HOME with onboarding and folder trust seeded, so it never touches a real login or
local state. Delivery is checked against the pane's own transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.cli.installers.shared import sync_bundled_content_to_db
from gobby.storage.hub.protocol import HubDatabase
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance
from tests.e2e.gmcp_client import gmcp_session

CLAUDE = shutil.which("claude") or ""
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
PAYLOAD_LEN = 2600
WORKSPACE_TOOLS = ("create_workspace", "create_tab", "send_text", "read_pane", "close_workspace")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_LIVE_CLAUDE_PROOF") != "1" or not CLAUDE,
        reason="live Claude proof needs GOBBY_LIVE_CLAUDE_PROOF=1 and a claude binary",
    ),
]


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    result = sync_bundled_content_to_db(postgres_db)
    assert result["errors"] == []


def _payload(head_id: str, tail_id: str) -> str:
    head = f"GOBBYPROOF-HEAD-{head_id} Reply with the single word OK and nothing else. "
    tail = f" GOBBYPROOF-TAIL-{tail_id}."
    fill = "abcdefghijklmnopqrstuvwxyz 0123456789 "
    middle_len = PAYLOAD_LEN - len(head) - len(tail)
    middle = (fill * (middle_len // len(fill) + 1))[:middle_len]
    payload = head + middle + tail
    assert len(payload) == PAYLOAD_LEN
    assert "\n" not in payload and "\r" not in payload
    return payload


def _find_str(obj: Any, keys: tuple[str, ...]) -> str | None:
    """Breadth-first search for the first string under one of ``keys``."""
    queue = [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str):
                    return value
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def _user_text(entry: dict[str, Any]) -> str | None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts) if parts else None
    return None


def _entries(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]


async def _wait_for[T](probe: Callable[[], Awaitable[T | None]], what: str, timeout: float) -> T:
    """Poll ``probe`` until it yields a value; fail the test if none appears in time."""
    deadline = time.monotonic() + timeout
    while True:
        found = await probe()
        if found is not None:
            return found
        if time.monotonic() > deadline:
            pytest.fail(f"{what} not seen within {timeout:.0f}s")
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_send_text_2600_reaches_live_claude_pane_whole(
    daemon_instance: DaemonInstance, cli_events: CLIEventSimulator, tmp_path: Path
) -> None:
    version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, check=False)

    project = cli_events.register_test_project(
        project_id=PROJECT_ID, name="Live Claude proof", repo_path=str(daemon_instance.project_dir)
    )
    assert project["status"] in {"success", "already_exists"}, project

    head_id = uuid.uuid4().hex[:8]
    tail_id = uuid.uuid4().hex[:8]
    payload = _payload(head_id, tail_id)

    cwd = tmp_path / "pane-cwd"
    cwd.mkdir()
    mcp_config = tmp_path / "mcp-empty.json"
    mcp_config.write_text('{"mcpServers": {}}')
    pane_home = tmp_path / "pane-home"
    (pane_home / ".claude").mkdir(parents=True)
    (pane_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "numStartups": 1,
                "projects": {
                    str(cwd.resolve()): {
                        "hasTrustDialogAccepted": True,
                        "allowedTools": [],
                        "mcpServers": {},
                    }
                },
            }
        )
    )
    daemon_url = f"http://127.0.0.1:{daemon_instance.http_port}"
    # env -i drops the inherited CLAUDE_CODE_* markers, which switch transcript saving off.
    launch = (
        f'cd {cwd} && env -i HOME={pane_home} PATH="$PATH" TERM=xterm-256color '
        f"LANG=en_US.UTF-8 DISABLE_AUTOUPDATER=1 GOBBY_HOME={daemon_instance.gobby_home} "
        f"GOBBY_DAEMON_URL={daemon_url} {CLAUDE} --strict-mcp-config "
        f"--mcp-config {mcp_config} --debug-file {tmp_path / 'claude-debug.txt'}; "
        f"echo EXIT=$? >> {tmp_path / 'claude-exit.txt'}"
    )

    terminal_context = {
        "parent_pid": os.getpid(),
        "tmux_pane": f"%proof-{head_id}",
        "tmux_socket_path": f"/tmp/proof-{head_id}.sock",
        "tty": f"/dev/pts/proof-{head_id}",
        "term_program": "pytest",
        "term_session_id": f"proof-{head_id}",
    }
    caller = f"claude-proof-caller-{head_id}"

    def rebind() -> None:
        # The caller stub is re-bound before each wrapper call; the proof targets the pane.
        result = cli_events.user_prompt_submit(
            caller,
            prompt="hello",
            source="claude",
            project_id=PROJECT_ID,
            cwd=str(daemon_instance.project_dir),
            terminal_context=terminal_context,
        )
        assert result.get("continue") is True, result

    start = cli_events.session_start(
        caller,
        cli_source="claude",
        session_start_source="startup",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    assert start.get("continue") is True, start
    rebind()

    async with gmcp_session(
        daemon_instance.gobby_home,
        cwd=daemon_instance.project_dir,
        daemon_url=daemon_url,
        terminal_context=terminal_context,
        identity_env={"GOBBY_PROJECT_ID": PROJECT_ID},
    ) as proxy:
        for tool in WORKSPACE_TOOLS:
            await proxy.get_tool_schema("gobby-workspaces", tool)

        async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            rebind()
            return await proxy.call_tool("gobby-workspaces", tool, arguments)

        workspace = await call("create_workspace", {"name": f"proof-{head_id}"})
        workspace_ref = _find_str(workspace, ("id",))
        assert workspace_ref, json.dumps(workspace)[:600]

        tab = await call(
            "create_tab",
            {"workspace": workspace_ref, "project_id": PROJECT_ID, "title": "proof"},
        )
        pane = _find_str(tab, ("pane_id", "pane_ref", "pane"))
        assert pane, json.dumps(tab)[:600]

        try:
            await call("send_text", {"pane": pane, "text": launch, "submit": True})

            async def composer_screen() -> str | None:
                read = await call("read_pane", {"pane": pane, "lines": 80})
                screen = json.dumps(read, ensure_ascii=False)
                composer = (
                    "Claude Code v" in screen and "Enter to confirm" not in screen and "❯" in screen
                )
                return screen if "for shortcuts" in screen or composer else None

            await _wait_for(composer_screen, "Claude composer", 150)

            started = time.time()
            await call("send_text", {"pane": pane, "text": payload, "submit": True})

            head = f"GOBBYPROOF-HEAD-{head_id}"
            projects = pane_home / ".claude" / "projects"

            async def prompt_entries() -> tuple[Path, list[dict[str, Any]]] | None:
                for path in projects.glob("*/*.jsonl"):
                    if path.stat().st_mtime < started - 5:
                        continue
                    try:
                        entries = _entries(path)
                    except json.JSONDecodeError:
                        continue  # the transcript line is still being written
                    matches = [
                        entry
                        for entry in entries
                        if entry.get("type") == "user" and head in (_user_text(entry) or "")
                    ]
                    if matches:
                        return path, matches
                return None

            transcript, matches = await _wait_for(
                prompt_entries, "the prompt in a Claude transcript", 180
            )
            assert len(matches) == 1, len(matches)
            received = _user_text(matches[0]) or ""
            report = {
                "claude_version": (version.stdout + version.stderr).strip(),
                "payload_len": len(payload),
                "received_len": len(received),
                "exact_match": received == payload,
                "head_marker_present": head in received,
                "tail_marker_present": f"GOBBYPROOF-TAIL-{tail_id}" in received,
                "transcript": str(transcript),
                "session_id": matches[0].get("sessionId"),
            }
            (tmp_path / "report.json").write_text(json.dumps(report, indent=2))
            print("REPORT", json.dumps(report, ensure_ascii=False))
            assert received == payload, (len(received), received[:160], received[-160:])
        finally:
            await call("close_workspace", {"workspace": workspace_ref})
