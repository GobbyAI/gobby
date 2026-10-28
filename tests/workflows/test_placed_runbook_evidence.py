"""Teardown evidence for the live steward: each copy is kept, and a text read does not abort it."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.native_bin import native_bin_name
from tests.workflows.test_placed_runbook_live import (
    CODEX_RUST_LOG,
    Live,
    _grant_files,
    _has_terminal_text,
    _hook_registered,
    _install_codex_hooks,
    _kept,
    _LiveOutputPoller,
    _preserve_steward_screen,
    _stage_home_bin,
    _steward_session,
    _with_codex_debug_log,
)

pytestmark = pytest.mark.unit


class _ScriptedMCP:
    """Answers each tool from its script; a list answers its calls in order, the last one repeating."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        replies = self.script[tool]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _steward(mcp: _ScriptedMCP) -> Live:
    # A stand-in with only the fields the steward helpers read; the rest of Live is unused here.
    return cast(Live, SimpleNamespace(evidence={"recovery_steward": {"run": "run-9"}}, mcp=mcp))


def test_steward_screen_keeps_a_text_read_and_every_capture_page(tmp_path: Path) -> None:
    mcp = _ScriptedMCP(
        {
            "get_agent_live_output": [
                {"success": True, "result": "--- Last 20 lines of terminal output ---"}
            ],
            "get_agent_result": [{"success": True, "result": {"status": "failed"}}],
            "get_agent_capture": [
                {"success": True, "result": {"content": "first page ", "next_offset": 11}},
                {"success": True, "result": {"content": "second page", "next_offset": None}},
            ],
        }
    )

    summary = _preserve_steward_screen(_steward(mcp), tmp_path, "probe")

    copy = tmp_path / "probe.steward-screen.json"
    assert summary == {"copy": str(copy), "run": "run-9"}
    record = json.loads(copy.read_text(encoding="utf-8"))
    assert record["get_agent_live_output"]["result"].startswith("--- Last 20 lines")
    assert record["get_agent_result"]["result"] == {"status": "failed"}
    assert [page["result"]["content"] for page in record["capture"]] == [
        "first page ",
        "second page",
    ]
    assert [args for tool, args in mcp.calls if tool == "get_agent_capture"] == [
        {"run_id": "run-9", "offset": 0},
        {"run_id": "run-9", "offset": 11},
    ]


def test_a_refused_read_is_recorded_and_the_other_copies_still_run(tmp_path: Path) -> None:
    mcp = _ScriptedMCP(
        {
            "get_agent_live_output": [httpx.ConnectError("daemon refused the read")],
            "get_agent_result": [{"success": True, "result": {"status": "failed"}}],
            "get_agent_capture": [{"success": False, "error": "capture_not_found"}],
        }
    )

    _preserve_steward_screen(_steward(mcp), tmp_path, "probe")

    record = json.loads((tmp_path / "probe.steward-screen.json").read_text(encoding="utf-8"))
    assert "daemon refused the read" in record["get_agent_live_output"]["error"]
    assert record["get_agent_result"]["result"] == {"status": "failed"}
    assert record["capture"] == [{"success": False, "error": "capture_not_found"}]


def test_a_failed_copy_is_recorded_in_place_and_the_next_copy_runs() -> None:
    def broken() -> dict[str, Any]:
        raise AssertionError("text terminal read")

    assert _kept(broken) == {"error": "AssertionError: text terminal read"}
    assert _kept(lambda: {"copy": "ok"}) == {"copy": "ok"}


def test_the_steward_host_env_asks_codex_for_its_debug_log() -> None:
    base = {"PATH": "/bin", "RUST_LOG": "info"}

    env = _with_codex_debug_log(base)

    assert env == {"PATH": "/bin", "RUST_LOG": CODEX_RUST_LOG}
    assert base == {"PATH": "/bin", "RUST_LOG": "info"}


@pytest.mark.parametrize(
    ("text", "has_text"),
    [
        ("--- Last 20 lines of terminal output ---", False),
        ("--- Last 20 lines of terminal output ---\n\n  \n", False),
        ("--- Last 20 lines of terminal output ---\nCodex exited", True),
    ],
)
def test_a_live_read_has_terminal_text_only_past_its_header(text: str, has_text: bool) -> None:
    assert _has_terminal_text(text) is has_text


class _ScriptedReader:
    """Answers from its script; the last reply sets the stop event, so the poll loop ends."""

    def __init__(self, replies: list[Any], stop: threading.Event) -> None:
        self.replies = replies
        self.stop = stop
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any], /) -> Any:
        self.calls.append((server, tool, arguments))
        reply = self.replies.pop(0)
        if not self.replies:
            self.stop.set()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        self.closed = True


def _capture(output: str) -> dict[str, Any]:
    return {"success": True, "result": {"success": True, "output": output}}


def test_live_output_poller_keeps_the_last_read_with_terminal_text() -> None:
    stop = threading.Event()
    reader = _ScriptedReader(
        [
            _capture(""),
            _capture("Codex exited"),
            _capture("  \n"),
            RuntimeError("daemon refused the read"),
        ],
        stop,
    )
    poller = _LiveOutputPoller(lambda: reader, lambda: "child-9", stop, 0)

    poller.start()
    assert stop.wait(timeout=10), "the poller never consumed its script"
    poller.stop()

    assert poller.record() == {
        "polls": 3,
        "last_nonempty": "Codex exited",
        "error": "RuntimeError: daemon refused the read",
        "stopped": True,
    }
    assert reader.calls[0] == ("gobby-sessions", "capture_output", {"session_id": "child-9"})
    assert reader.closed


class _FakeHub:
    """Answers the one steward lookup with its row."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        return self.row


class _RunHub:
    """Answers the steward run's child binding, and the child's session row."""

    def __init__(self, child: str, *, external_id: str) -> None:
        self.child = child
        self.external_id = external_id

    def fetchone(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        if "agent_runs" in sql:
            return {"child_session_id": self.child}
        return {"id": self.child, "external_id": self.external_id}


def test_the_steward_session_is_known_once_its_run_binds_a_child() -> None:
    evidence = {"recovery_steward": {"run": "run-9"}}
    unbound = cast(HubDatabase, _FakeHub({"child_session_id": None}))
    bound = cast(HubDatabase, _FakeHub({"child_session_id": "child-9"}))

    assert _steward_session(unbound, evidence) is None
    assert _steward_session(bound, evidence) == "child-9"
    assert _steward_session(bound, {}) is None


def test_grant_files_are_listed_by_path_and_their_contents_are_not_read(tmp_path: Path) -> None:
    (tmp_path / "runs" / "a").mkdir(parents=True)
    (tmp_path / "runs" / "a" / "grant.json").write_text('{"token": "x"}', encoding="utf-8")

    assert _grant_files(tmp_path) == ["runs/a/grant.json"]


def _installed(native: Path, name: str, content: bytes) -> None:
    native.mkdir(exist_ok=True)
    binary = native / native_bin_name(name)
    binary.write_bytes(content)
    binary.chmod(0o755)


def test_gmcp_and_ghook_are_staged_into_the_home_bin_with_their_hashes(tmp_path: Path) -> None:
    native = tmp_path / "native"
    _installed(native, "gmcp", b"gmcp-build")
    _installed(native, "ghook", b"ghook-build")
    home = tmp_path / "home"
    home.mkdir()

    recorded = _stage_home_bin(home, native, ("gmcp", "ghook"))

    for name, content in (("gmcp", b"gmcp-build"), ("ghook", b"ghook-build")):
        staged = home / "bin" / native_bin_name(name)
        assert staged.read_bytes() == content
        assert os.access(staged, os.X_OK)
        assert recorded[name] == hashlib.sha256(content).hexdigest()


def test_staging_refuses_a_native_set_missing_one_of_the_binaries(tmp_path: Path) -> None:
    native = tmp_path / "native"
    _installed(native, "gmcp", b"gmcp-build")
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(FileNotFoundError):
        _stage_home_bin(home, native, ("gmcp", "ghook"))


def test_codex_hooks_install_into_the_home_and_name_the_staged_ghook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _installed(home / "bin", "ghook", b"ghook-build")

    installed = _install_codex_hooks(home, tmp_path / "project")

    assert installed["success"] is True
    hooks = (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert str(home / "bin" / native_bin_name("ghook")) in hooks
    assert "SessionStart" in hooks


def test_the_steward_child_counts_as_registered_only_once_its_external_id_changes() -> None:
    evidence = {"recovery_steward": {"run": "run-9"}}
    registered = cast(HubDatabase, _RunHub("child-9", external_id="codex-thread-9"))
    unregistered = cast(HubDatabase, _RunHub("child-9", external_id="child-9"))

    assert _hook_registered(registered, evidence) == {
        "id": "child-9",
        "external_id": "codex-thread-9",
    }
    with pytest.raises(AssertionError):
        _hook_registered(unregistered, evidence)
    with pytest.raises(AssertionError):
        _hook_registered(registered, {})
