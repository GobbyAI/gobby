"""Teardown evidence for the live steward: each copy is kept, and a text read does not abort it."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.workflows.test_placed_runbook_live import (
    CODEX_RUST_LOG,
    STEWARD_CODEX_FLAGS,
    STEWARD_LAUNCH_RECORD,
    Live,
    _codex_argv_wrapper,
    _grant_files,
    _has_terminal_text,
    _kept,
    _launch_record,
    _link_codex_auth,
    _LiveOutputPoller,
    _prepare_launch_record_dir,
    _preserve_steward_screen,
    _settled_screen,
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


def test_the_steward_session_is_known_once_its_run_binds_a_child() -> None:
    evidence = {"recovery_steward": {"run": "run-9"}}
    unbound = cast(HubDatabase, _FakeHub({"child_session_id": None}))
    bound = cast(HubDatabase, _FakeHub({"child_session_id": "child-9"}))

    assert _steward_session(unbound, evidence) is None
    assert _steward_session(bound, evidence) == "child-9"
    assert _steward_session(bound, {}) is None


def test_the_steward_wrapper_records_what_codex_inherits_and_runs_codex_with_the_flags_last(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "codex"
    fake.write_text('#!/bin/sh\necho "codex $*"\n', encoding="utf-8")
    fake.chmod(0o755)
    record = tmp_path / "log" / STEWARD_LAUNCH_RECORD
    record.parent.mkdir()
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(
        _codex_argv_wrapper(str(fake), STEWARD_CODEX_FLAGS, record), encoding="utf-8"
    )
    wrapper.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "GOBBY_HOME": "/home/steward",
        "GOBBY_AGENT_API_TOKEN": "secret-value",
        "TMPDIR": "/scratch",
    }
    seat_argv = ["-c", 'mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN="secret-value"', "a prompt a=b"]

    result = subprocess.run(
        [str(wrapper), *seat_argv], env=env, capture_output=True, text=True, check=True
    )

    assert result.stdout.strip().splitlines()[-1] == (
        'codex -c mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN="secret-value" '
        "a prompt a=b -c mcp_servers.gobby.required=false"
    )
    text = record.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "arg mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN" in lines
    assert "arg mcp_servers.gobby.required" in lines
    assert "env GOBBY_AGENT_API_TOKEN" in lines
    assert "value GOBBY_HOME=/home/steward" in lines
    assert "value TMPDIR=/scratch" in lines
    assert "value GOBBY_MACHINE_ID=" in lines
    assert "secret-value" not in text
    assert not any("prompt" in line for line in lines)


def test_the_steward_launch_record_names_the_spawn_overrides_it_did_not_inherit(
    tmp_path: Path,
) -> None:
    log = tmp_path / "providers" / "codex" / "log"
    log.mkdir(parents=True)
    (log / STEWARD_LAUNCH_RECORD).write_text(
        "arg mcp_servers.gobby.required\nenv GOBBY_HOME\nvalue GOBBY_HOME=/home/steward\n",
        encoding="utf-8",
    )

    record = _launch_record(tmp_path)

    assert record["overrides"] == ["mcp_servers.gobby.required"]
    assert record["env_names"] == ["GOBBY_HOME"]
    assert record["values"] == {"GOBBY_HOME": "/home/steward"}
    missing = record["production_overrides_missing"]
    assert "mcp_servers.gobby.env.GOBBY_HOME" in missing
    assert "shell_environment_policy.set.GOBBY_HOME" in missing


def test_a_steward_without_a_launch_record_says_so(tmp_path: Path) -> None:
    assert _launch_record(tmp_path) == {
        "missing": str(tmp_path / "providers" / "codex" / "log" / STEWARD_LAUNCH_RECORD)
    }


def test_grant_files_are_listed_by_path_and_their_contents_are_not_read(tmp_path: Path) -> None:
    (tmp_path / "runs" / "a").mkdir(parents=True)
    (tmp_path / "runs" / "a" / "grant.json").write_text('{"token": "x"}', encoding="utf-8")

    assert _grant_files(tmp_path) == ["runs/a/grant.json"]


def test_the_steward_screen_settles_only_once_the_composer_shows_and_stops_changing() -> None:
    mcp = _ScriptedMCP(
        {
            "capture_output": [
                _capture("› Working 1s"),
                _capture("› Working 2s"),
                _capture("› ready"),
                _capture("› ready"),
                _capture("done"),
                _capture("done"),
            ]
        }
    )
    live = cast(Live, SimpleNamespace(mcp=mcp))

    assert _settled_screen(live, "child-9", pause=0) is None
    assert _settled_screen(live, "child-9", pause=0) == "› ready"
    assert _settled_screen(live, "child-9", pause=0) is None


def test_the_launch_record_dir_is_made_after_the_auth_link_and_the_link_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A placeholder source, so no operator login is read or copied.
    source = tmp_path / "codex-source"
    source.mkdir(mode=0o700)
    (source / "auth.json").write_text("{}", encoding="utf-8")
    (source / "auth.json").chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(source))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    _link_codex_auth(home)
    record = _prepare_launch_record_dir(home)

    assert record == home / "providers" / "codex" / "log" / STEWARD_LAUNCH_RECORD
    assert record.parent.is_dir()
    assert (home / "providers" / "codex" / "auth.json").is_symlink()
    assert (home / ".codex").is_symlink()
    assert (home / "providers").stat().st_mode & 0o777 == 0o700
    # Before the link, providers does not exist: the record dir must fail rather than make it.
    with pytest.raises(FileNotFoundError):
        _prepare_launch_record_dir(tmp_path / "unlinked")
