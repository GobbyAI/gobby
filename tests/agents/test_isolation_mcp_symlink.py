"""Isolation repair must refuse tree JSON config that is not a regular tree file."""

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from gobby.agents import isolation_repair
from gobby.agents.isolation_repair import _patch_mcp_config_for_isolation
from gobby.utils.nofollow_dirs import open_nofollow_dir

_SENTINEL = "sentinel-8f3c1a"


def _regular_files_containing(root: Path, needle: str) -> list[Path]:
    hits: list[Path] = []
    for directory, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            candidate = Path(directory) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if needle in candidate.read_text(errors="replace"):
                hits.append(candidate)
    return hits


@pytest.mark.asyncio
async def test_mcp_repair_refuses_symlink_out_of_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside.json"
    original = '{"secret": "' + _SENTINEL + '"}\n'
    outside.write_text(original)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    config = isolated / ".mcp.json"
    config.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        await _patch_mcp_config_for_isolation(str(tmp_path), str(isolated), "codex")

    assert config.is_symlink()
    assert os.readlink(config) == str(outside)
    assert outside.read_text() == original
    assert _regular_files_containing(isolated, _SENTINEL) == []


@pytest.mark.parametrize("linked", ["file", "parent"])
def test_droid_hooks_repair_refuses_links_out_of_the_tree(tmp_path: Path, linked: str) -> None:
    outside_dir = tmp_path / "outside"
    (outside_dir / "hooks").mkdir(parents=True)
    outside = outside_dir / "hooks" / "hooks.json"
    original = '{"secret": "' + _SENTINEL + '"}\n'
    outside.write_text(original)
    workspace = tmp_path / "isolated"
    workspace.mkdir()
    hooks_path = workspace / ".factory" / "hooks" / "hooks.json"
    if linked == "file":
        hooks_path.parent.mkdir(parents=True)
        hooks_path.symlink_to(outside)
    else:
        (workspace / ".factory").symlink_to(outside_dir)

    with pytest.raises(ValueError, match="symbolic link|linked parent"):
        isolation_repair._write_droid_isolation_hooks(workspace, hooks_path)

    assert outside.read_text() == original
    assert sorted(entry.name for entry in (outside_dir / "hooks").iterdir()) == ["hooks.json"]
    assert _regular_files_containing(workspace, _SENTINEL) == []


def _swap_after_open(
    monkeypatch: pytest.MonkeyPatch, call: int, swap: Callable[[], None]
) -> list[int]:
    """Make the ``call``-th parent open run the real walk, then swap the tree under it."""
    real = open_nofollow_dir
    calls: list[int] = []

    def racing_open(root: Path, parts: Sequence[str], *, create: bool = False) -> int:
        descriptor = real(root, parts, create=create)
        calls.append(len(calls) + 1)
        if len(calls) == call:
            swap()
        return descriptor

    monkeypatch.setattr(isolation_repair, "open_nofollow_dir", racing_open)
    return calls


def _outside_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    outside_dir = tmp_path / "outside"
    (outside_dir / "hooks").mkdir(parents=True)
    outside = outside_dir / "hooks" / "hooks.json"
    original = '{"secret": "' + _SENTINEL + '"}\n'
    outside.write_text(original)
    return outside_dir, outside, original


@pytest.mark.parametrize("call", [1, 2], ids=["read", "write"])
def test_droid_hooks_repair_holds_parent_against_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    outside_dir, outside, original = _outside_tree(tmp_path)
    workspace = tmp_path / "isolated"
    hooks_path = workspace / ".factory" / "hooks" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text('{"hooks": {}}\n')
    factory = workspace / ".factory"
    moved = workspace / ".factory-checked"

    def swap() -> None:
        factory.rename(moved)
        factory.symlink_to(outside_dir)

    calls = _swap_after_open(monkeypatch, call, swap)
    if call == 1:
        # The read used the held descriptor; the write's fresh walk meets the link.
        with pytest.raises(ValueError, match="linked parent"):
            isolation_repair._write_droid_isolation_hooks(workspace, hooks_path)
    else:
        # The write stays on the held, now-renamed directory inside the tree.
        isolation_repair._write_droid_isolation_hooks(workspace, hooks_path)
        assert "hooks" in json.loads((moved / "hooks" / "hooks.json").read_text())

    assert len(calls) >= call
    assert outside.read_text() == original
    assert sorted(entry.name for entry in (outside_dir / "hooks").iterdir()) == ["hooks.json"]
    assert _regular_files_containing(workspace, _SENTINEL) == []


@pytest.mark.parametrize("call", [1, 2], ids=["read", "write"])
@pytest.mark.asyncio
async def test_mcp_repair_holds_tree_against_leaf_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    outside_dir, outside, original = _outside_tree(tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    config = isolated / ".mcp.json"
    config.write_text('{"mcpServers": {}}\n')

    def swap() -> None:
        config.unlink()
        config.symlink_to(outside)

    _swap_after_open(monkeypatch, call, swap)
    if call == 1:
        with pytest.raises(ValueError, match="symbolic link"):
            await _patch_mcp_config_for_isolation(str(tmp_path), str(isolated), "codex")
    else:
        await _patch_mcp_config_for_isolation(str(tmp_path), str(isolated), "codex")
        assert not config.is_symlink()

    assert outside.read_text() == original
    assert sorted(entry.name for entry in (outside_dir / "hooks").iterdir()) == ["hooks.json"]
    assert _regular_files_containing(isolated, _SENTINEL) == []


@pytest.mark.asyncio
async def test_mcp_repair_refuses_non_regular_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    config = isolated / ".mcp.json"
    os.mkfifo(config)

    with pytest.raises(ValueError, match="not a regular file"):
        await _patch_mcp_config_for_isolation(str(tmp_path), str(isolated), "codex")

    assert not config.is_file()
    assert [entry.name for entry in isolated.iterdir()] == [".mcp.json"]
