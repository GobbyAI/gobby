"""Construction rollback drains a spawned host and preserves an adopted one."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.runner_rollback import (
    rollback_runner_resources,
    rollback_runner_resources_async,
)

pytestmark = pytest.mark.unit


class _Host:
    def __init__(self, *, adopted: bool) -> None:
        self.adopted = adopted
        self.spawned_this_construction = not adopted
        self.events: list[str] = []
        self.drain_gate: Any = None
        self.closed_clients = False
        self.stopped_producers = False

    async def stop_producers(self) -> None:
        self.stopped_producers = True
        self.events.append("producers")

    async def rollback_host(self) -> None:
        if self.drain_gate is not None:
            await self.drain_gate.wait()
        if self.spawned_this_construction:
            self.events.append("drain")
        else:
            self.events.append("preserve")

    async def close_clients(self) -> None:
        self.closed_clients = True
        self.events.append("clients")


def _runner(host: _Host) -> Any:
    return MagicMock(
        terminal_host_manager=host,
        http_server=None,
        config_runtime=None,
        definition_revision_listener=None,
        memory_manager=None,
        vector_store=None,
        database=None,
        db_executor=None,
        worktree_delete_executor=None,
        coverage_executor=None,
        database_watchdog=None,
    )


@pytest.mark.asyncio
async def test_host_supervisor_rollback_drains_spawned_preserves_adopted() -> None:
    spawned = _Host(adopted=False)
    await rollback_runner_resources_async(_runner(spawned))
    assert spawned.events[0] == "producers"
    assert "drain" in spawned.events
    assert spawned.closed_clients

    adopted = _Host(adopted=True)
    await rollback_runner_resources_async(_runner(adopted))
    assert "preserve" in adopted.events
    assert "drain" not in adopted.events
    assert adopted.closed_clients


@pytest.mark.asyncio
async def test_terminal_rollback_orders_drain_before_client_close() -> None:
    spawned = _Host(adopted=False)
    await rollback_runner_resources_async(_runner(spawned))
    assert spawned.events == ["producers", "drain", "clients"]

    adopted = _Host(adopted=True)
    await rollback_runner_resources_async(_runner(adopted))
    assert adopted.events == ["producers", "preserve", "clients"]

    # Independent settle of clients-before-drain is a failing contrast.
    source = inspect.getsource(rollback_runner_resources_async)
    tree = ast.parse(source)
    # The helper must not schedule independent settle tasks for host drain.
    assert "_settle_async_close" not in source
    assert any(isinstance(node, ast.Await) for node in ast.walk(tree))


def test_terminal_rollback_awaited_before_exit() -> None:
    sync_source = inspect.getsource(rollback_runner_resources)
    assert "asyncio.run" in sync_source

    create_src = Path(__file__).resolve().parents[1] / "src" / "gobby" / "runner.py"
    create_tree = ast.parse(create_src.read_text())
    lifecycle_src = Path(__file__).resolve().parents[1] / "src" / "gobby" / "runner_lifecycle.py"
    lifecycle_text = lifecycle_src.read_text()
    lifecycle_tree = ast.parse(lifecycle_text)

    def _awaits_rollback(tree: ast.AST) -> bool:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id == "rollback_runner_resources_async":
                    return True
        return False

    create_fn = next(
        node
        for node in ast.walk(create_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create"
    )
    assert _awaits_rollback(create_fn)
    assert _awaits_rollback(lifecycle_tree)
    assert "cleanup_owned_pid_file" in lifecycle_text
    gate_idx = lifecycle_text.index("rollback_runner_resources_async")
    pid_idx = lifecycle_text.index("cleanup_owned_pid_file", gate_idx)
    assert pid_idx > gate_idx


def test_init_uses_sync_wrapper() -> None:
    runner_src = Path(__file__).resolve().parents[1] / "src" / "gobby" / "runner.py"
    tree = ast.parse(runner_src.read_text())
    init_calls: list[str] = []
    create_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            init_calls.extend(
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            )
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create":
            create_calls.extend(
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            )
            create_calls.extend(
                n.func.attr
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            )
    assert "rollback_runner_resources" in init_calls
    assert "rollback_runner_resources_async" in create_calls
