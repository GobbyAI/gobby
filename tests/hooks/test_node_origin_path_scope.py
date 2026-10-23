"""Node facts are authoritative even when hub paths disagree."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks._normalization_git import classify_git_mutation
from gobby.hooks._path_scope import (
    _git_common_dir,
    _is_project_managed_path,
    checkout_root,
    current_project_root,
    resolve_tool_path,
)
from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.checkout_reads import CheckoutReadUnavailable, delivery_scope
from gobby.hooks.events import HookResponse
from gobby.providers.registry import provider_metadata
from gobby.servers.routes.mcp.hook_responses import _hook_exception_response
from gobby.workflows.condition_helpers_paths import write_paths_within
from gobby.workflows.enforcement.blocking import plan_write_paths_allowed
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PROVIDER_DIRECTORIES = tuple(
    dict.fromkeys(metadata.user_directory for metadata in provider_metadata())
)


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    for root in ("/home/node", "/tmp", *(f"/home/node/{d}" for d in PROVIDER_DIRECTORIES)):
        fake.add_file(NODE_MACHINE, root, b"")
    return fake


def _fact(node: FakeNode, path: str, resolved: str | None = None) -> None:
    node.add_file(NODE_MACHINE, path, b"")
    if resolved is not None:
        node.files[NODE_MACHINE][path]["resolved"] = resolved


async def test_node_path_primitives_use_node_facts(node: FakeNode, tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    (hub / ".git").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(hub, target_is_directory=True)
    raw = str(alias / "file.py")
    _fact(node, raw, "/node/repo/file.py")
    _fact(node, "file.py", "/node/repo/file.py")
    node.add_outside(NODE_MACHINE, str(alias / "outside.py"))
    _fact(node, "/tmp/agent-scratchpad/file.py")
    node.set_checkout(NODE_MACHINE, str(alias), root="/node/repo", project_id="node-project")

    def observe() -> None:
        assert resolve_tool_path("file.py", alias) == Path("/node/repo/file.py")
        assert current_project_root({"project_path": str(alias)}) == alias
        assert current_project_root({"cwd": str(alias)}) == Path("/node/repo")
        assert checkout_root(Path(raw)) == Path("/checkout")
        assert _git_common_dir(Path(raw)) == Path("/checkout/.git")
        assert not _is_project_managed_path(alias / "outside.py", alias)
        assert _is_project_managed_path(Path("/tmp/agent-scratchpad/file.py"), alias)
        assert resolve_tool_path("", alias) is None

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(alias)):
        await asyncio.to_thread(observe)
    alias.unlink()
    alias.mkdir()
    (alias / ".git").write_text("gitdir: /wrong/hub/gitdir")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(alias)):
        await asyncio.to_thread(observe)


@pytest.mark.parametrize(
    "command",
    [
        ["git", "checkout", "--force", "main"],
        ["git", "restore", "--unknown-option"],
        ["git", "restore", "--pathspec-from-file=paths.txt"],
    ],
)
async def test_git_mutation_checkout_root_by_origin(
    node: FakeNode, tmp_path: Path, command: list[str]
) -> None:
    hub = tmp_path / "hub"
    (hub / ".git").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(hub, target_is_directory=True)
    _fact(node, str(alias))
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(alias)):
        result = await asyncio.to_thread(classify_git_mutation, command, str(alias), alias)
    assert result is not None
    assert result.paths == ("/checkout",)
    local = classify_git_mutation(command, str(alias), alias)
    assert local is not None
    assert local.paths == (str(hub),)


async def test_node_write_scope(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    data = {"cwd": cwd, "project_path": cwd}
    _fact(node, "docs", "/node/repo/docs")
    _fact(node, "docs/ok.md", "/node/repo/docs/ok.md")
    _fact(node, "docs/link.md", "/node/elsewhere/file.md")
    _fact(node, "docs-other/file.md", "/node/repo/docs-other/file.md")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert await asyncio.to_thread(
            write_paths_within, data, {"file_path": "docs/ok.md"}, ["docs"]
        )
        assert not await asyncio.to_thread(
            write_paths_within, data, {"file_path": "docs/link.md"}, ["docs"]
        )
        assert not await asyncio.to_thread(
            write_paths_within, data, {"file_path": "docs-other/file.md"}, ["docs"]
        )
        assert not await asyncio.to_thread(write_paths_within, data, {}, ["docs"])
    node.fail(NODE_MACHINE, "node_unavailable")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert not await asyncio.to_thread(
            write_paths_within, data, {"file_path": "docs/ok.md"}, ["docs"]
        )


async def test_node_plan_mode_writes(node: FakeNode, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    _fact(node, cwd, "/node/repo")
    _fact(node, ".gobby/plans/x.md", "/node/repo/.gobby/plans/x.md")
    _fact(node, "~/notes.md", "/home/node/.claude/notes.md")
    _fact(node, "source.py", "/node/repo/source.py")
    _fact(node, "plan-link.md", "/node/repo/source.py")

    def allowed(*paths: str, current: bool = False) -> bool:
        return plan_write_paths_allowed(
            {"file_paths": list(paths)}, "claude", ".gobby/plans/x.md", current, project_path=cwd
        )

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert await asyncio.to_thread(allowed, ".gobby/plans/x.md", "~/notes.md")
        assert await asyncio.to_thread(allowed, ".gobby/plans/x.md", current=True)
        assert not await asyncio.to_thread(allowed, "source.py")
        assert not await asyncio.to_thread(allowed, ".gobby/plans/x.md", "source.py")
        assert not await asyncio.to_thread(allowed, "plan-link.md")
    node.fail(NODE_MACHINE, "node_unavailable")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        for path in (".gobby/plans/x.md", "~/notes.md", "source.py"):
            assert not await asyncio.to_thread(allowed, path)


async def test_node_plan_scratch_resolves_on_node_only(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hub-only links: node-private -> seat-cache and node-temp -> hub-wide.
    seat_cache = tmp_path / "seat-cache"
    hub_wide = tmp_path / "hub-wide"
    seat_cache.mkdir()
    hub_wide.mkdir()
    (tmp_path / "node-private").symlink_to(seat_cache)
    (tmp_path / "node-temp").symlink_to(hub_wide)
    cwd = str(tmp_path)
    _fact(node, cwd, "/node/repo")

    def allowed(*paths: str) -> bool:
        return plan_write_paths_allowed(
            {"file_paths": list(paths)}, "claude", ".gobby/plans/x.md", False, project_path=cwd
        )

    monkeypatch.setattr("tests.hooks.checkout_read_fakes._TEMP_DIR", str(seat_cache))
    _fact(node, str(seat_cache), str(seat_cache))
    _fact(node, "seat.md", str(seat_cache / "seat.md"))
    _fact(node, "private.md", str(tmp_path / "node-private" / "config.md"))
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert await asyncio.to_thread(allowed, "seat.md")
        assert not await asyncio.to_thread(allowed, "private.md")

    monkeypatch.setattr("tests.hooks.checkout_read_fakes._TEMP_DIR", str(tmp_path / "node-temp"))
    _fact(node, str(tmp_path / "node-temp"), str(tmp_path / "node-temp"))
    _fact(node, "notes.md", str(tmp_path / "node-temp" / "notes.md"))
    _fact(node, "wide.md", str(hub_wide / "wide.md"))
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert await asyncio.to_thread(allowed, "notes.md")
        assert not await asyncio.to_thread(allowed, "wide.md")


@pytest.mark.parametrize(
    ("temp", "codex_root"),
    [
        pytest.param("/home/node/cache", "/home/node/cache/codex", id="inside-home"),
        pytest.param("/node/tmp", "/node/tmp/codex", id="outside-home"),
    ],
)
async def test_node_plan_scratch_rejects_inactive_provider_root_symlink(
    node: FakeNode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp: str,
    codex_root: str,
) -> None:
    # Active provider is claude. The node's inactive .codex root links into temp.
    cwd = str(tmp_path)
    monkeypatch.setattr("tests.hooks.checkout_read_fakes._TEMP_DIR", temp)
    _fact(node, cwd, "/node/repo")
    _fact(node, temp, temp)
    _fact(node, "/home/node/.codex", codex_root)
    _fact(node, ".codex/settings.json", f"{codex_root}/settings.json")
    _fact(node, f"{codex_root}/settings.json", f"{codex_root}/settings.json")
    _fact(node, f"{temp}/notes.md", f"{temp}/notes.md")
    _fact(node, "/home/node/.claude/scratch.md", "/home/node/.claude/scratch.md")

    def allowed(*paths: str) -> bool:
        return plan_write_paths_allowed(
            {"file_paths": list(paths)}, "claude", ".gobby/plans/x.md", False, project_path=cwd
        )

    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        assert not await asyncio.to_thread(allowed, ".codex/settings.json")
        assert not await asyncio.to_thread(allowed, f"{codex_root}/settings.json")
        assert await asyncio.to_thread(allowed, f"{temp}/notes.md")
        assert await asyncio.to_thread(allowed, "/home/node/.claude/scratch.md")


async def test_unavailable_normalization_fails_closed(node: FakeNode, tmp_path: Path) -> None:
    node.fail(NODE_MACHINE, "node_unavailable")
    adapter = ClaudeCodeAdapter()
    manager = MagicMock()
    manager.handle.return_value = HookResponse(decision="allow")
    payload = {
        "_platform_session_id": "node-path-scope-session",
        "hook_type": "pre-tool-use",
        "source": "claude",
        "input_data": {
            "machine_id": NODE_MACHINE,
            "cwd": str(tmp_path),
            "project_path": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": "source.py", "content": "changed"},
        },
    }
    try:
        result = await run_adapter_hook(adapter, payload, manager, timeout_seconds=5)
    except CheckoutReadUnavailable as exc:
        # The HTTP route uses this same exception-to-response boundary.
        result = _hook_exception_response(
            adapter, "pre-tool-use", "claude", {"critical": True}, str(exc)
        )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "checkout_read_unavailable: node_unavailable" in str(result)
    assert NODE_MACHINE in str(result)
    manager.handle.assert_not_called()
