"""Replay harness for the adapter corpus.

Each replay builds its adapter (or app) afresh, so a class or module attribute patched before
the call is the one the replay runs. The kinds:

- ``inbound``: the native envelope (``hook_type``, ``input_data``, ``source``) goes through the
  adapter's ``translate_to_hook_event``; the expect block holds the event it builds.
- ``outbound``: a ``HookResponse`` built from ``response`` goes through
  ``translate_from_hook_response`` for ``hook_type``; the expect block holds the provider output.
- ``node_origin``: an inbound translation inside a node delivery (``delivery_scope`` with a
  machine id that is not this one), for a payload that names a file that does not exist.
- ``git_verdict``: the guard payload goes through ``GitAdapter`` inbound, then the case's verdict
  goes back out through ``translate_from_hook_response``.
- ``route_refusal``: ``request`` is posted to the hooks route of a fresh app; the expect block
  holds the status, the detail, the sources the detail names and whether any adapter
  translation ran.

Outputs are stored scrubbed (``rules_recorder._jsonable``): timestamps, UUIDs and host paths are
replaced with placeholders, so a replay on another machine or day yields the same block.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.git import GitAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.hooks import checkout_reads
from gobby.hooks.events import HookResponse
from gobby.servers.routes.mcp.hooks import create_hooks_router
from gobby.utils import machine_id as machine_id_module
from tests.contracts.rules_recorder import RECORD_ENV, _jsonable

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

ADAPTERS: dict[str, Any] = {
    "claude": ClaudeCodeAdapter,
    "codex": CodexHooksAdapter,
    "droid": DroidAdapter,
    "grok": GrokAdapter,
    "agy": AgyAdapter,
    "git": GitAdapter,
}
TRANSLATION_METHODS = ("translate_to_hook_event", "translate_from_hook_response")
# The hub's machine id during a replay, and the paired node a node-origin case comes from.
HUB_MACHINE_ID = "7a1e2b3c-4d5e-4f60-8172-839405162738"
NODE_MACHINE_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"
GIT_VERDICT_DECISIONS: dict[str, Literal["allow", "block"]] = {"allow": "allow", "block": "block"}
_SUPPORTED = re.compile(r"Supported: (.*)$")


def _envelope(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hook_type": case["hook_type"],
        "input_data": copy.deepcopy(case["payload"]),
        "source": case["source"],
    }


def _event_output(adapter: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    event = adapter.translate_to_hook_event(_envelope(case))
    return {"event": _jsonable(event)}


@contextlib.contextmanager
def _hub_machine() -> Iterator[None]:
    with mock.patch.object(machine_id_module, "_cached_machine_id", HUB_MACHINE_ID):
        yield


def _node_output(case: Mapping[str, Any]) -> dict[str, Any]:
    loop = asyncio.new_event_loop()
    try:
        cwd = str(case["payload"].get("cwd") or "/")
        with checkout_reads.delivery_scope(NODE_MACHINE_ID, loop, cwd):
            return _event_output(ADAPTERS[case["source"]](), case)
    finally:
        loop.close()


def _outbound_output(case: Mapping[str, Any]) -> dict[str, Any]:
    response = HookResponse(**copy.deepcopy(case["response"]))
    adapter = ADAPTERS[case["source"]]()
    output = adapter.translate_from_hook_response(response, hook_type=case["hook_type"])
    return {"output": _jsonable(output)}


def _git_output(case: Mapping[str, Any]) -> dict[str, Any]:
    adapter = GitAdapter()
    observed = _event_output(adapter, case)
    response = HookResponse(
        decision=GIT_VERDICT_DECISIONS[case["verdict"]],
        reason=case.get("reason"),
    )
    output = adapter.translate_from_hook_response(response, hook_type=case["hook_type"])
    return {**observed, "output": _jsonable(output)}


def _counting(calls: list[str], name: str, method: Callable[..., Any]) -> Callable[..., Any]:
    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return method(*args, **kwargs)

    return counted


def _refusal_output(case: Mapping[str, Any]) -> dict[str, Any]:
    calls: list[str] = []
    app = FastAPI()
    # The route refuses these requests before it needs a real server, so a stand-in serves.
    server = cast("HTTPServer", SimpleNamespace(services=None))
    app.include_router(create_hooks_router(server))
    app.state.hook_manager = SimpleNamespace()
    with contextlib.ExitStack() as stack:
        for name, adapter in ADAPTERS.items():
            for method in TRANSLATION_METHODS:
                current = getattr(adapter, method)
                stack.enter_context(
                    mock.patch.object(adapter, method, _counting(calls, name, current))
                )
        client = stack.enter_context(TestClient(app, raise_server_exceptions=True))
        reply = client.post("/api/hooks/execute", json=case["request"])
    detail = str(reply.json().get("detail", ""))
    named = _SUPPORTED.search(detail)
    return {
        "status": reply.status_code,
        "detail": detail,
        "supported_sources_named": named.group(1).split(", ") if named else [],
        "adapter_called": bool(calls),
    }


def replay_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Run one case through the Python adapter (or the hooks route) and return its output."""
    kind = case["kind"]
    with _hub_machine():
        if kind == "inbound":
            return _event_output(ADAPTERS[case["source"]](), case)
        if kind == "node_origin":
            return _node_output(case)
        if kind == "outbound":
            return _outbound_output(case)
        if kind == "git_verdict":
            return _git_output(case)
        if kind == "route_refusal":
            return _refusal_output(case)
    raise ValueError(f"{case.get('id')}: unknown case kind {kind!r}")


def record_corpus(corpus_dir: Path, writer: Callable[[Path, dict[str, Any]], None]) -> None:
    """Fill each case's ``expect`` block from the Python adapters.

    Runs only when ``GOBBY_CORPUS_RECORD`` is 1; otherwise the corpus files are left unchanged.
    """
    import json

    if os.environ.get(RECORD_ENV) != "1":
        return
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        for case in document["cases"]:
            case["expect"] = replay_case(case)
        writer(path, document)
