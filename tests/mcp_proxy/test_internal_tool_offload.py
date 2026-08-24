"""Internal MCP tools must not block the event loop (#20845, item 5).

`InternalToolRegistry.call` offloads a sync tool with `asyncio.to_thread` and
awaits a coroutine tool inline, so a tool declared `async def` that never awaits
runs its whole body -- synchronous psycopg included -- on the loop thread. The
loop-stack sampler caught exactly that: a 2.36s stall whose hot stacks were
`_execute_tool -> InternalToolRegistry.call -> create_task -> resolve_session_id
-> resolve_session_reference -> PostgresHubDatabase.fetchone -> ...
-> ConnectionPool.getconn -> assert_runtime_role -> Connection.wait`.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "src" / "gobby" / "mcp_proxy" / "tools"

# Tools here coordinate a loop-scheduled background run rather than only doing
# blocking work: the reuse path checks whether a run's asyncio.Task already
# exists, and a tool offloaded to a worker thread can create that task only
# through call_soon_threadsafe, so it is not there yet when the next call looks.
# Moving them off the loop means reworking when a run becomes observable, which
# is #20855's change, not this guard's.
_LOOP_COORDINATING_MODULES = frozenset({Path("tasks/_expansion_registry.py")})


def _awaits_something(node: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(inner, ast.Await | ast.AsyncFor | ast.AsyncWith) for inner in ast.walk(node)
    )


def _registered_by_call(tree: ast.Module) -> set[str]:
    """Names handed to `registry.register(...)` as a callable in this module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if called not in {"register", "register_tool"}:
            continue
        for keyword in node.keywords:
            if keyword.arg == "func" and isinstance(keyword.value, ast.Name):
                names.add(keyword.value.id)
        for arg in node.args:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
    return names


def _registered_by_decorator(node: ast.AsyncFunctionDef) -> bool:
    """True for the `@registry.tool(...)` registration form."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in {
            "tool",
            "register",
            "register_tool",
        }:
            return True
    return False


def _fake_async_tools() -> list[str]:
    """Registered tools declared `async def` whose body never awaits."""
    found: list[str] = []
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        if path.relative_to(_TOOLS_ROOT) in _LOOP_COORDINATING_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        registered = _registered_by_call(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or _awaits_something(node):
                continue
            if node.name not in registered and not _registered_by_decorator(node):
                continue
            found.append(f"{path.relative_to(_TOOLS_ROOT.parents[3])}:{node.lineno} {node.name}")
    return found


def test_no_internal_tool_is_declared_async_without_awaiting() -> None:
    """The guard for the whole class, not one chain of it.

    A tool that awaits nothing gains nothing from `async def` and loses the
    registry's offload, so the declaration alone decides whether its blocking
    work lands on the loop. 41 registered tools were in this state when the
    loop-stack sampler named one of them.
    """
    offenders = _fake_async_tools()

    assert offenders == [], (
        "declared async but never awaits, so InternalToolRegistry.call runs the "
        "body on the event loop thread:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_a_sync_tool_body_runs_off_the_event_loop_thread() -> None:
    """What the declaration buys: the registry's `asyncio.to_thread` hop."""
    registry = InternalToolRegistry(name="test-offload", description="")
    ran_on: list[int] = []

    def blocking_tool() -> str:
        ran_on.append(threading.get_ident())
        return "done"

    registry.register(
        name="blocking_tool",
        description="",
        input_schema={"type": "object", "properties": {}},
        func=blocking_tool,
    )
    loop_thread = threading.get_ident()

    assert await registry.call("blocking_tool", {}) == "done"

    assert ran_on == [ran_on[0]]
    assert ran_on[0] != loop_thread, "a sync tool body ran on the event loop thread"


@pytest.mark.asyncio
async def test_an_awaiting_tool_still_runs_inline_on_the_loop() -> None:
    """Genuinely async tools keep the cheaper path; the guard is about the
    tools that only look async."""
    registry = InternalToolRegistry(name="test-inline", description="")
    ran_on: list[int] = []

    async def genuinely_async() -> None:
        return None

    async def awaiting_tool() -> str:
        await genuinely_async()
        ran_on.append(threading.get_ident())
        return "done"

    registry.register(
        name="awaiting_tool",
        description="",
        input_schema={"type": "object", "properties": {}},
        func=awaiting_tool,
    )
    loop_thread = threading.get_ident()

    assert await registry.call("awaiting_tool", {}) == "done"

    assert ran_on == [loop_thread]
