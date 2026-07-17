"""Builtin ToolRuntime contracts: schemas, limits, sizing, deadlines, and trace."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from gobby.ai import _tool_chat_tools as tools
from gobby.ai._tool_chat_adapters import build_repo_mcp_server
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
    canonical_json_size,
    minimum_typed_error_result_size,
    serialize_builtin_tool_result,
    serialized_builtin_tool_result_size,
)
from gobby.ai._tool_chat_contracts import (
    ToolLoopConfigurationError,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.ai._tool_chat_tools import ToolPolicyError, ToolRuntime
from gobby.tasks.diff_paging import DiffPagingError

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit_bytes": {"type": "integer", "minimum": 4, "maximum": 100},
    },
    "required": ["limit_bytes"],
    "additionalProperties": False,
}


def _policy() -> ToolPolicy:
    return ToolPolicy(cli="gcode", tools=("search",))


def _spec(
    handler: Callable[
        [dict[str, Any], BuiltinExecutionContext], Coroutine[Any, Any, BuiltinToolResult]
    ],
    *,
    name: str = "read_page",
    schema: dict[str, Any] | None = None,
) -> BuiltinToolSpec:
    return BuiltinToolSpec(
        name=name,
        description="Read one evidence page.",
        input_schema=schema or _INPUT_SCHEMA,
        handler=handler,
    )


async def _success_handler(
    arguments: dict[str, Any], context: BuiltinExecutionContext
) -> BuiltinToolResult:
    return BuiltinToolResult(
        payload={"arguments": arguments, "capacity": context.max_payload_bytes}
    )


def test_builtin_schema_renders_on_openai_and_repo_mcp_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_tool(
        name: str, description: str, input_schema: dict[str, Any]
    ) -> Callable[[object], object]:
        captured[name] = {"description": description, "input_schema": input_schema}
        return lambda handler: handler

    def fake_server(**kwargs: object) -> dict[str, object]:
        captured["server"] = kwargs
        return {"fake": True}

    monkeypatch.setattr("claude_agent_sdk.tool", fake_tool)
    monkeypatch.setattr("claude_agent_sdk.create_sdk_mcp_server", fake_server)
    runtime = ToolRuntime(_policy(), project_path="/repo", builtins=(_spec(_success_handler),))

    schemas = runtime.openai_schemas()
    builtin_schema = next(item for item in schemas if item["function"]["name"] == "read_page")
    assert builtin_schema["function"]["parameters"] == _INPUT_SCHEMA

    _, allowed = build_repo_mcp_server(runtime)
    assert captured["read_page"] == {
        "description": "Read one evidence page.",
        "input_schema": _INPUT_SCHEMA,
    }
    assert "mcp__repo__read_page" in allowed


@pytest.mark.parametrize("name", ["bad name", "x" * 65, "", "bad.name"])
def test_builtin_name_rejects_provider_incompatible_syntax(name: str) -> None:
    with pytest.raises(ToolPolicyError, match="must match"):
        ToolRuntime(_policy(), project_path="/repo", builtins=(_spec(_success_handler, name=name),))


def test_builtin_names_must_be_unique_and_not_collide_with_cli_names() -> None:
    duplicate = _spec(_success_handler)
    with pytest.raises(ToolPolicyError, match="duplicated"):
        ToolRuntime(_policy(), project_path="/repo", builtins=(duplicate, duplicate))

    with pytest.raises(ToolPolicyError, match="collides"):
        ToolRuntime(
            _policy(),
            project_path="/repo",
            builtins=(_spec(_success_handler, name="gcode_search"),),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_bytes", [-1, 3, 101])
async def test_builtin_schema_rejects_out_of_range_limits_without_clamping(
    limit_bytes: int,
) -> None:
    called = False

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        nonlocal called
        called = True
        return await _success_handler(arguments, context)

    runtime = ToolRuntime(_policy(), project_path="/repo", builtins=(_spec(handler),))

    result = json.loads(await runtime.execute("read_page", {"limit_bytes": limit_bytes}))

    assert result["success"] is False
    assert result["error_code"] == "invalid_tool_arguments"
    assert called is False
    assert runtime.calls_used == 1


@pytest.mark.asyncio
async def test_exact_cap_result_uses_preallocated_ref_and_shared_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_ref = "e" * 32
    events: list[str] = []
    payload = {"text": "évidence", "items": [1, 2, 3]}
    expected_result = BuiltinToolResult(
        payload=payload,
        selector={"commit": "abc", "path": "src/x.py"},
        range={"start": 0, "end": 12},
        complete=True,
        content_hash="hash",
    )
    cap = serialized_builtin_tool_result_size(expected_result, evidence_ref=evidence_ref)

    def allocate() -> str:
        events.append("allocated")
        return evidence_ref

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        events.append("handled")
        assert arguments == {"limit_bytes": 12}
        assert context.evidence_ref == evidence_ref
        assert context.max_payload_bytes == canonical_json_size(payload)
        return expected_result

    monkeypatch.setattr(tools, "new_evidence_ref", allocate)
    runtime = ToolRuntime(
        _policy(),
        project_path="/repo",
        limits=ToolLoopLimits(per_tool_result_byte_cap=cap),
        builtins=(_spec(handler),),
    )

    text = await runtime.execute("read_page", {"limit_bytes": 12})

    assert events == ["allocated", "handled"]
    assert text == serialize_builtin_tool_result(expected_result, evidence_ref=evidence_ref)
    assert len(text.encode("utf-8")) == cap
    assert runtime.invocation_log == [
        {
            "tool_name": "read_page",
            "arguments": {"limit_bytes": 12},
            "result_size_bytes": cap,
            "ok": True,
            "error_code": None,
            "evidence_ref": evidence_ref,
            "selector": {"commit": "abc", "path": "src/x.py"},
            "range": {"start": 0, "end": 12},
            "complete": True,
            "content_hash": "hash",
        }
    ]


@pytest.mark.asyncio
async def test_oversized_builtin_result_returns_typed_size_error() -> None:
    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        return BuiltinToolResult(payload={"text": "x" * (context.max_payload_bytes + 1)})

    runtime = ToolRuntime(
        _policy(),
        project_path="/repo",
        limits=ToolLoopLimits(per_tool_result_byte_cap=100),
        builtins=(_spec(handler),),
    )

    text = await runtime.execute("read_page", {"limit_bytes": 4})
    result = json.loads(text)

    assert result == {
        "error": "result exceeds cap",
        "error_code": "tool_result_too_large",
        "success": False,
    }
    assert len(text.encode("utf-8")) <= 100
    assert runtime.invocation_log[0]["evidence_ref"] is None


@pytest.mark.asyncio
async def test_unserializable_builtin_result_returns_typed_error_without_raising() -> None:
    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        return BuiltinToolResult(payload=object())

    runtime = ToolRuntime(_policy(), project_path="/repo", builtins=(_spec(handler),))

    result = json.loads(await runtime.execute("read_page", {"limit_bytes": 4}))

    assert result == {
        "error": "builtin result is not JSON serializable",
        "error_code": "invalid_tool_result",
        "success": False,
    }
    record = runtime.invocation_log[0]
    assert record["ok"] is False
    assert record["error_code"] == "invalid_tool_result"
    assert record["evidence_ref"] is None


def test_result_cap_rejects_one_below_minimum_and_accepts_minimum() -> None:
    minimum = minimum_typed_error_result_size()

    with pytest.raises(ToolLoopConfigurationError, match=f"at least {minimum}"):
        ToolLoopLimits(per_tool_result_byte_cap=minimum - 1)

    assert ToolLoopLimits(per_tool_result_byte_cap=minimum).per_tool_result_byte_cap == minimum


@pytest.mark.parametrize("timeout", [0, -1.0])
def test_nonpositive_outer_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ToolLoopConfigurationError, match="must be positive"):
        ToolLoopLimits(tool_timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_typed_handler_failure_never_raises_through_runtime() -> None:
    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        raise DiffPagingError("git_timeout", "git process timed out", command="diff")

    runtime = ToolRuntime(_policy(), project_path="/repo", builtins=(_spec(handler),))

    result = json.loads(await runtime.execute("read_page", {"limit_bytes": 4}))

    assert result == {
        "details": {"command": "diff"},
        "error": "git process timed out",
        "error_code": "git_timeout",
        "success": False,
    }


@pytest.mark.asyncio
async def test_runtime_owns_shared_call_budget_and_typed_refusal() -> None:
    runtime = ToolRuntime(
        _policy(),
        project_path="/repo",
        limits=ToolLoopLimits(max_tool_calls=1),
        builtins=(_spec(_success_handler),),
    )

    await runtime.execute("read_page", {"limit_bytes": 4})
    refusal = json.loads(await runtime.execute("gcode_search", {"args": ["auth"]}))

    assert runtime.calls_used == 1
    assert runtime.budget_exhausted is True
    assert refusal["error_code"] == "tool_call_budget_exhausted"
    assert runtime.invocation_log[-1]["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "wait_for_deadline"),
    [(1.0, True), (5.0, True), (60.0, False)],
)
async def test_layered_deadline_reaps_child_and_worker_before_return(
    timeout: float,
    wait_for_deadline: bool,
) -> None:
    observed: dict[str, Any] = {}
    worker_done = threading.Event()
    deadline_waiter = threading.Event()

    def worker(context: BuiltinExecutionContext) -> BuiltinToolResult:
        observed["remaining"] = context.subprocess_deadline - time.monotonic()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        observed["process"] = process
        try:
            if wait_for_deadline:
                deadline_waiter.wait(
                    timeout=max(0.0, context.subprocess_deadline - time.monotonic())
                )
        finally:
            process.kill()
            process.wait(timeout=5)
            worker_done.set()
        return BuiltinToolResult(error_code="git_timeout", error="git process timed out")

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        return await asyncio.to_thread(worker, context)

    runtime = ToolRuntime(
        _policy(),
        project_path="/repo",
        limits=ToolLoopLimits(tool_timeout_seconds=timeout),
        builtins=(_spec(handler),),
    )

    result = json.loads(await runtime.execute("read_page", {"limit_bytes": 4}))

    expected_inner = timeout - min(5.0, timeout / 2)
    assert observed["remaining"] == pytest.approx(expected_inner, abs=0.1)
    assert result["error_code"] == "git_timeout"
    assert observed["process"].poll() is not None
    assert worker_done.is_set()
    assert runtime._builtin_tasks == set()


@pytest.mark.asyncio
async def test_outer_timeout_awaits_child_cleanup_and_worker_completion() -> None:
    worker_done = threading.Event()
    release_worker = threading.Event()
    observed: dict[str, subprocess.Popen[bytes]] = {}

    def worker() -> BuiltinToolResult:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        observed["process"] = process
        try:
            release_worker.wait()
        finally:
            process.kill()
            process.wait(timeout=5)
            worker_done.set()
        return BuiltinToolResult(payload={"late": True})

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        return await asyncio.to_thread(worker)

    runtime = ToolRuntime(
        _policy(),
        project_path="/repo",
        limits=ToolLoopLimits(tool_timeout_seconds=0.1),
        builtins=(_spec(handler),),
    )
    asyncio.get_running_loop().call_later(0.15, release_worker.set)

    result = json.loads(await runtime.execute("read_page", {"limit_bytes": 4}))

    assert result["error_code"] == "tool_timeout"
    assert worker_done.is_set()
    assert observed["process"].poll() is not None
    assert runtime._builtin_tasks == set()
