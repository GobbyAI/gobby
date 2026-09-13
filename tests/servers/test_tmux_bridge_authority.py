"""Acceptance 2.5.10 / 4.3.5: web attach shares the daemon lease without a PTY bridge."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_bridge_shares_lease_and_leaves_external_options_alone() -> None:
    registry = TerminalLeaseRegistry()
    attach = await registry.attach("term-ext", frame_delivery="proxy")
    refused = registry.admit_write(
        "term-ext",
        attachment_id=attach.attachment_id,
        expected_lease_generation=0,
        seq=1,
        kind="input",
        payload=b"x",
    )
    assert refused.ok is False
    gobby = await registry.attach("term-gobby", frame_delivery="proxy")
    granted = await registry.take_control("term-gobby", gobby.attachment_id, takeover=False)
    assert granted.granted is True


def test_async_lease_mutation_callers_are_awaited() -> None:
    repo = Path(__file__).resolve().parents[2]
    paths = (
        "src/gobby/servers/websocket/proxy_relay.py",
        "src/gobby/servers/websocket/terminal_ws.py",
        "src/gobby/servers/websocket/terminal_ws_control.py",
        "src/gobby/servers/websocket/tmux.py",
        "src/gobby/servers/websocket/tmux_activation.py",
        "tests/servers/test_terminal_list_watermark.py",
        "tests/servers/test_terminal_ws_golden.py",
        "tests/servers/test_terminal_ws_input.py",
        "tests/servers/test_terminal_ws_lease.py",
        "tests/servers/test_terminal_ws_resize.py",
        "tests/servers/test_terminal_ws_viewport.py",
        "tests/servers/test_tmux_bridge_authority.py",
        "tests/terminals/test_lease_authority.py",
        "tests/terminals/test_native_runtime.py",
        "tests/terminals/test_write_coordinator.py",
        "tests/terminals/test_write_outcomes.py",
    )
    mutations = {
        "attach",
        "take_control",
        "release_control",
        "finalize",
        "finalize_websocket",
    }
    violations: list[str] = []
    for relative in paths:
        source = (repo / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in mutations
            ):
                continue
            receiver = ast.unparse(node.func.value)
            if "lease" not in receiver and receiver != "registry":
                continue
            parent = parents[node]
            scheduled = (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr in {"create_task", "gather"}
            )
            if not isinstance(parent, ast.Await) and not scheduled:
                violations.append(f"{relative}:{node.lineno}:{node.func.attr}")
    assert violations == []
