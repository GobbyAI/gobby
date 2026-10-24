"""Code-navigation verification reads the delivery's node checkout."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.hooks import code_navigation_recovery as recovery
from gobby.hooks.checkout_reads import delivery_scope
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


async def test_node_line_count_reads_node_bytes(node: FakeNode, tmp_path: Path) -> None:
    short = tmp_path / "short.py"
    large = tmp_path / "large.py"
    node.add_file(NODE_MACHINE, str(short), b"line\n" * 40)
    node.add_file(NODE_MACHINE, str(large), b"x" * (recovery._LINE_COUNT_SCAN_LIMIT + 2))
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        count = await asyncio.to_thread(recovery._verified_source_line_count, [short])
        oversized = await asyncio.to_thread(recovery._verified_source_line_count, [large])
    assert count == 40
    assert oversized is None
    assert not short.exists()
    assert not large.exists()
    queries = [payload["query"] for _, _, payload, _ in node.requests]
    assert queries == [
        {"kind": "files", "paths": [str(short)], "read_limit": 262145},
        {"kind": "files", "paths": [str(large)], "read_limit": 262145},
    ]


async def test_node_ignore_decision_uses_node_config(node: FakeNode, tmp_path: Path) -> None:
    root = tmp_path / "linked"
    path = root / ".hidden" / "module.py"
    config = root / ".gobby" / "gcode.json"
    node.add_file(NODE_MACHINE, str(path), b"node\n")
    node.files[NODE_MACHINE][str(path)]["checkout_root"] = str(root)
    node.add_file(NODE_MACHINE, str(config), b'{"index":{"hidden_allowlist":[".hidden/**"]}}')
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        decision = await asyncio.to_thread(recovery._ignore_decision, path, tmp_path)
    assert decision is False
    queries = [payload["query"] for _, _, payload, _ in node.requests]
    assert queries == [
        {"kind": "files", "paths": [str(path)], "read_limit": None},
        {"kind": "files", "paths": [str(config)], "read_limit": 65536},
    ]
    assert not root.exists()


async def test_unanswered_node_keeps_enforcement(node: FakeNode, tmp_path: Path) -> None:
    path = tmp_path / ".hidden" / "module.py"
    path.parent.mkdir()
    path.write_text("hub decoy\n")
    config = tmp_path / ".gobby" / "gcode.json"
    config.parent.mkdir()
    config.write_text('{"index":{"hidden_allowlist":[".hidden/**"]}}')
    node.fail(NODE_MACHINE, "node_disconnected")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        count = await asyncio.to_thread(recovery._verified_source_line_count, [path])
        decision = await asyncio.to_thread(recovery._ignore_decision, path, tmp_path)
    assert count is None
    assert decision == tmp_path
    assert len(node.requests) == 1
