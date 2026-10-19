"""Rust test-writing classification reads the delivering node's checkout."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.hooks.checkout_reads import delivery_scope
from gobby.workflows.rust_test_evidence import rust_edit_is_test_writing
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BEFORE = "pub fn value() -> u8 { 1 }\n#[cfg(test)]\nmod tests {\n}\n"
_OLD = "mod tests {\n}"
_NEW = "mod tests {\n    #[test]\n    fn value_is_one() { assert_eq!(super::value(), 1); }\n}"
_TEST_ONLY = "#[cfg(test)]\nmod tests { #[test] fn works() { assert_eq!(1, 1); } }\n"


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


async def test_node_rust_test_edit_reads_node_text(node: FakeNode, tmp_path: Path) -> None:
    path = str(tmp_path / "node_only.rs")
    assert not Path(path).exists()
    node.add_file(NODE_MACHINE, path, _BEFORE.encode() + b"// invalid UTF-8: \xff\n")

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        result = await asyncio.to_thread(
            rust_edit_is_test_writing,
            path,
            {"file_path": path, "old_string": _OLD, "new_string": _NEW},
        )

    assert result is True
    assert node.requests == [
        (
            NODE_MACHINE,
            "checkout.read",
            {
                "cwd": str(tmp_path),
                "query": {"kind": "files", "paths": [path], "read_limit": 8_388_608},
            },
            5000,
        )
    ]


@pytest.mark.parametrize("failure", ["unavailable", "truncated", "outside"])
@pytest.mark.parametrize("write", [False, True], ids=["edit", "write"])
async def test_unanswered_node_is_not_test_only(
    node: FakeNode, tmp_path: Path, failure: str, write: bool
) -> None:
    path = str(tmp_path / "module.rs")
    # A readable hub file must never substitute for an unanswered node read.
    Path(path).write_text(_BEFORE)
    node.add_file(NODE_MACHINE, path, _BEFORE.encode())
    if failure == "unavailable":
        node.fail(NODE_MACHINE, "offline")
    elif failure == "truncated":
        node.files[NODE_MACHINE][path]["truncated"] = True
    else:
        node.files[NODE_MACHINE][path]["kind"] = "outside"
    tool_input = (
        {"file_path": path, "content": _TEST_ONLY}
        if write
        else {"file_path": path, "old_string": _OLD, "new_string": _NEW}
    )

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        result = await asyncio.to_thread(rust_edit_is_test_writing, path, tool_input)

    assert result is False
    assert len(node.requests) == 1


async def test_node_missing_file_allows_new_test_only_write(node: FakeNode, tmp_path: Path) -> None:
    path = str(tmp_path / "new.rs")
    # The hub's production text cannot influence a new file on the node.
    Path(path).write_text("pub fn unrelated() {}\n")
    node.add_file(NODE_MACHINE, path, b"")
    node.files[NODE_MACHINE][path].update(kind="missing", data=None)

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        result = await asyncio.to_thread(
            rust_edit_is_test_writing, path, {"file_path": path, "content": _TEST_ONLY}
        )

    assert result is True
    assert len(node.requests) == 1
