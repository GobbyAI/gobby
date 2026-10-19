"""Monolith guard reads node files for node-origin deliveries (#24526)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.hooks.checkout_reads import delivery_scope
from gobby.workflows.monolith_guard import outstanding_monolith_paths, projected_monolith_paths
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = pytest.mark.unit

_READ_LIMIT = 8_388_608
_NODE_ONLY_ROOT = "/node-only/checkout"


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


def _lines(count: int) -> str:
    return "".join(f"line {index}\n" for index in range(count))


@pytest.mark.asyncio
@pytest.mark.parametrize("hub_copy", [True, False], ids=["hub-copy", "node-only-root"])
async def test_node_projection_reads_node_file(
    node: FakeNode, tmp_path: Path, hub_copy: bool
) -> None:
    root = str(tmp_path) if hub_copy else _NODE_ONLY_ROOT
    path = f"{root}/src/gobby/big.py"
    node.add_file(NODE_MACHINE, path, _lines(990).encode())
    if hub_copy:
        hub_file = tmp_path / "src" / "gobby" / "big.py"
        hub_file.parent.mkdir(parents=True)
        hub_file.write_text(_lines(2_000), encoding="utf-8")
    loop = asyncio.get_running_loop()

    with delivery_scope(NODE_MACHINE, loop, root):
        crossing = await asyncio.to_thread(
            projected_monolith_paths, {"file_path": path, "content": _lines(1_000)}, root
        )
        kept = await asyncio.to_thread(
            projected_monolith_paths, {"file_path": path, "content": _lines(995)}, root
        )

    assert crossing == ["src/gobby/big.py"]
    assert kept == []
    assert node.requests[0][2]["query"] == {
        "kind": "files",
        "paths": [path],
        "read_limit": _READ_LIMIT,
    }


@pytest.mark.asyncio
async def test_node_ledger_recount_batches(node: FakeNode) -> None:
    root = _NODE_ONLY_ROOT
    small = [f"src/gobby/small_{index}.py" for index in range(2_000)]
    for relative in small:
        node.add_file(NODE_MACHINE, f"{root}/{relative}", _lines(3).encode())
    node.add_file(NODE_MACHINE, f"{root}/src/gobby/big.py", _lines(1_000).encode())
    variables = {
        "task_edited_files": {
            "task-a": [*small[:1_500], "src/gobby/big.py"],
            "task-b": [*small[1_000:], "src/gobby/big.py"],
        }
    }
    loop = asyncio.get_running_loop()

    with delivery_scope(NODE_MACHINE, loop, root):
        outstanding = await asyncio.to_thread(outstanding_monolith_paths, variables, root)

    assert outstanding == ["src/gobby/big.py"]
    batches = [request[2]["query"] for request in node.requests]
    assert [len(query["paths"]) for query in batches] == [1_000, 1_000, 1]
    assert {query["read_limit"] for query in batches} == {_READ_LIMIT}


@pytest.mark.asyncio
async def test_unanswered_node_lists_diagnostic(node: FakeNode) -> None:
    root = _NODE_ONLY_ROOT
    loop = asyncio.get_running_loop()
    offline = {"task_edited_files": {"task-a": ["src/gobby/a.py", "docs/notes.md"]}}

    node.fail(NODE_MACHINE, "node_offline")
    with delivery_scope(NODE_MACHINE, loop, root):
        unanswered = await asyncio.to_thread(outstanding_monolith_paths, offline, root)
    with delivery_scope(NODE_MACHINE, loop, root):
        written = await asyncio.to_thread(
            projected_monolith_paths,
            {"file_path": f"{root}/src/gobby/a.py", "content": _lines(5)},
            root,
        )
    diagnostic = f"checkout_read_unavailable: node_offline (machine {NODE_MACHINE})"
    assert unanswered == [f"src/gobby/a.py ({diagnostic})"]
    assert written == [f"src/gobby/a.py ({diagnostic})"]

    node.recover(NODE_MACHINE)
    node.add_file(NODE_MACHINE, f"{root}/src/gobby/partial.py", _lines(10).encode())
    node.add_file(NODE_MACHINE, f"{root}/src/gobby/huge.py", _lines(1_000).encode())
    for name in ("partial.py", "huge.py"):
        node.files[NODE_MACHINE][f"{root}/src/gobby/{name}"]["truncated"] = True
    truncated = {"task_edited_files": {"task-a": ["src/gobby/partial.py", "src/gobby/huge.py"]}}
    with delivery_scope(NODE_MACHINE, loop, root):
        listed = await asyncio.to_thread(outstanding_monolith_paths, truncated, root)

    assert listed == [
        f"src/gobby/partial.py (checkout_read_unavailable: truncated (machine {NODE_MACHINE}))",
        "src/gobby/huge.py",
    ]
