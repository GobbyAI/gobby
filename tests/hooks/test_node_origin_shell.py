"""Shell classification uses the delivery node's executable and destination facts."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks import shell_execution
from gobby.hooks._normalization_canonical import _normalize_shell_tool_metadata
from gobby.hooks._normalization_operands import _file_transfer_paths
from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.checkout_reads import CheckoutReadUnavailable, delivery_scope
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookResponse
from gobby.servers.routes.mcp.hook_responses import _hook_exception_response
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


@pytest.mark.parametrize("parked", [False, True])
async def test_node_executable_classified_from_node_bytes(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parked: bool
) -> None:
    script, native = tmp_path / "run.sh", tmp_path / "native"
    body = b"#!/bin/sh\ntouch result.txt\n"
    node.add_file(NODE_MACHINE, str(script), body)
    node.add_file(NODE_MACHINE, "./run.sh", body)
    node.files[NODE_MACHINE]["./run.sh"]["resolved"] = str(script)
    node.add_file(NODE_MACHINE, str(native), b"\xcf\xfa\xed\xfe\x00\x00\x00\x00")
    node.add_file(NODE_MACHINE, "result.txt", b"")
    node.files[NODE_MACHINE]["result.txt"].update(
        resolved=str(tmp_path / "result.txt"), kind="missing", data=None
    )
    assert not script.exists()
    assert not native.exists()
    if parked:
        reads: set[Future[tuple[Path, bytes] | None]] = {Future(), Future()}
        monkeypatch.setattr(shell_execution, "_parked_reads", reads)
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        execution = await asyncio.to_thread(shell_execution.path_execution, ["./run.sh"], tmp_path)
        assert execution is not None
        assert execution.script_word == str(script)
        assert execution.pipeline_subshell is True
        script_body = await asyncio.to_thread(
            shell_execution.read_script_body, str(script), tmp_path
        )
        assert script_body == (script, body.decode())
        native_execution = await asyncio.to_thread(
            shell_execution.path_execution, [str(native)], tmp_path
        )
        assert native_execution is None
        metadata = await asyncio.to_thread(
            _normalize_shell_tool_metadata, "./run.sh", base_cwd=tmp_path
        )
    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_file_paths"] == ["result.txt"]
    assert metadata["canonical_script_bodies"] == [str(script)]
    limits = {request[2]["query"]["read_limit"] for request in node.requests}
    assert limits == {None, 512, shell_execution.SCRIPT_BODY_LIMIT + 1}


@pytest.mark.parametrize("kind", ["dir", "file", "missing", "other", "outside"])
async def test_node_transfer_destination_kind(node: FakeNode, tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "dest"
    # Hub and node deliberately disagree about the destination's kind.
    if kind != "dir":
        destination.mkdir()
    node.add_file(NODE_MACHINE, str(destination), b"")
    node.files[NODE_MACHINE][str(destination)]["kind"] = kind
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        paths = await asyncio.to_thread(
            _file_transfer_paths, "cp", ["cp", "a.txt", "dest"], None, tmp_path
        )
    assert paths == (["dest/a.txt"] if kind == "dir" else ["dest"])
    assert len(node.requests) == 1
    assert node.requests[0][2]["query"]["paths"] == [str(destination)]


async def test_unanswered_node_shell_fails_closed(node: FakeNode, tmp_path: Path) -> None:
    node.fail(NODE_MACHINE, "node_unavailable")
    adapter = ClaudeCodeAdapter()
    manager = MagicMock()
    manager.handle.return_value = HookResponse(decision="allow")
    payload = {
        "_platform_session_id": "node-shell-session",
        "hook_type": "pre-tool-use",
        "source": "claude",
        "input_data": {
            "machine_id": NODE_MACHINE,
            "cwd": str(tmp_path),
            "project_path": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "./run.sh"},
        },
    }
    try:
        result = await run_adapter_hook(adapter, payload, manager, timeout_seconds=5)
    except CheckoutReadUnavailable as exc:
        result = _hook_exception_response(
            adapter, "pre-tool-use", "claude", {"critical": True}, str(exc)
        )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "checkout_read_unavailable: node_unavailable" in str(result)
    assert NODE_MACHINE in str(result)
    manager.handle.assert_not_called()


@pytest.mark.parametrize(
    ("command", "proven"),
    [
        ("cd /node/task && git commit -m fix", True),
        ("cd -- /node/task && touch out.txt", True),
        (
            "cd '/node/task tree' && UV_PROJECT_ENVIRONMENT=/venv PYTHONPATH=src "
            "uv run --no-sync ruff format file.py",
            True,
        ),
        ("git commit -m fix", False),
        ("cd /node/task && cd b && touch out.txt", False),
        ('cd "$X" && touch out.txt', False),
        ("cd relative && touch out.txt", False),
        ("cd /node/task > out.txt && touch out.txt", False),
        ("cd /node/task && touch out.txt; touch elsewhere.txt", False),
        ("cd /node/task && touch out.txt &", False),
        ("cd /node/task && command cd /other", False),
        ("cd /node/task && pushd /other", False),
        ("cd /node/task && popd", False),
        ("cd /node/task && (touch out.txt)", False),
        ("cd /node/task && touch $(cd /other; echo out.txt)", False),
        ("cd /node/task && bash -c 'touch out.txt'", False),
        ("cd /node/task && eval 'touch out.txt'", False),
        ("cd /node/task && source script.sh", False),
        ("cd /node/task && env -C /other touch out.txt", False),
        ("cd /node/task && sudo -D /other touch out.txt", False),
        ("cd /node/task && uv run --directory /other ruff format file.py", False),
        ("cd /node/task && UV_WORKING_DIR=/other uv run --no-sync ruff format file.py", False),
        (
            "cd /node/task && uv run --no-sync --env-file .env "
            "uv run --no-sync ruff format file.py",
            False,
        ),
        (
            "cd /node/task && UV_ENV_FILE=.env uv run --no-sync "
            "uv run --no-sync ruff format file.py",
            False,
        ),
        ("cd /node/task && uv run env -C /other touch out.txt", False),
        ("cd /node/task && touch out.txt | cat", False),
    ],
)
async def test_codex_node_shell_directory_proof(
    node: FakeNode, command: str, proven: bool, mock_dependencies: dict[str, Any]
) -> None:
    adapter = CodexHooksAdapter()
    handlers = EventHandlers(**mock_dependencies)
    for path in (
        "/node/task/out.txt",
        "/node/task/b/out.txt",
        "relative/out.txt",
        "/other/out.txt",
    ):
        node.add_file(NODE_MACHINE, path, b"")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), "/node/launcher"):
        event = await asyncio.to_thread(
            adapter.translate_to_hook_event,
            {
                "hook_type": "PreToolUse",
                "input_data": {
                    "machine_id": NODE_MACHINE,
                    "session_id": "native-node-session",
                    "cwd": "/node/launcher",
                    "transcript_path": "/node/provider/rollout.jsonl",
                    "tool_use_id": "native-shell-call",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
            },
        )
        assert event is not None
        assert event.data["tool_use_id"] == "native-shell-call"
        assert event.data["tool_input"]["command"] == command
        if proven:
            expected = "/node/task tree" if "task tree" in command else "/node/task"
            assert event.data["tool_input"]["workdir"] == expected
            assert event.metadata.get("_native_shell_cwd_unproven") is not True
        else:
            assert event.metadata["_native_shell_cwd_unproven"] is True
            response = await asyncio.to_thread(handlers.handle_before_tool, event)
            if command in {
                "cd /node/task && command cd /other",
                "cd /node/task && pushd /other",
                "cd /node/task && popd",
            }:
                # Directory-only reads can run, but never provide an ownership root.
                assert response.decision == "allow"
            else:
                assert response.decision == "block"
                assert "cd '/absolute/task/worktree' &&" in (response.reason or "")
