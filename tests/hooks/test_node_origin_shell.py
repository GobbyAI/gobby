"""Shell classification uses the delivery node's executable and destination facts."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks import shell_execution
from gobby.hooks._normalization_canonical import _normalize_shell_tool_metadata
from gobby.hooks._normalization_operands import _file_transfer_paths
from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.checkout_reads import CheckoutReadUnavailable, delivery_scope
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
