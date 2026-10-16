"""Clones fork the selected commit; the extracted merge path keeps its behavior."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import gobby.clones.git as clone_git
from gobby.clones.git import CloneGitManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A caller checkout with an origin remote that holds main."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Clone Fork Test")
    commit(root, "tracked.txt", "main bytes\n", "main")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "origin", "main")
    return root


@pytest.fixture
def clones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "clones"
    monkeypatch.setattr(clone_git, "CLONES_ROOT", root)
    return root


def clone_tool(repo: Path) -> tuple[Any, MagicMock]:
    from gobby.mcp_proxy.tools.clones import create_clones_registry

    storage = MagicMock()
    storage.create.side_effect = lambda **values: SimpleNamespace(
        to_dict=lambda: {"id": "clone", **values}
    )
    registry = create_clones_registry(
        clone_storage=storage,
        git_manager=CloneGitManager(repo),
        project_id="11111111-1111-4111-8111-111111110001",
    )
    return registry, storage


async def test_local_clone_uses_caller_head(repo: Path, clones_root: Path) -> None:
    git(repo, "switch", "-c", "caller")
    selected = commit(repo, "caller.txt", "unpushed\n", "unpushed caller commit")
    registry, storage = clone_tool(repo)
    path = clones_root / "child"

    result = await registry.call("create_clone", {"branch_name": "child", "clone_path": str(path)})

    assert result["success"] is True, result
    assert git(path, "rev-parse", "HEAD") == selected
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "child"
    assert (path / "caller.txt").read_text() == "unpushed\n"
    assert result["base_commit_sha"] == selected
    assert result["index_source"] == "local"
    assert storage.create.call_args.kwargs["base_branch"] == "caller"


@pytest.mark.parametrize("base", ["main", "origin/main", "external"])
async def test_clone_ref_sources(repo: Path, clones_root: Path, tmp_path: Path, base: str) -> None:
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "caller")
    commit(repo, "caller.txt", "caller only\n", "caller commit")
    publisher = tmp_path / "publisher"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(publisher))
    git(publisher, "config", "user.email", "test@example.com")
    git(publisher, "config", "user.name", "Publisher")
    remote_tip = commit(publisher, "remote.txt", "remote\n", "remote advances main")
    git(publisher, "push", "origin", "main")
    git(publisher, "push", "origin", "main:child")
    registry, storage = clone_tool(repo)
    path = clones_root / "child"
    arguments: dict[str, Any] = {"branch_name": "child", "clone_path": str(path)}
    if base == "external":
        arguments["remote_url"] = (tmp_path / "origin.git").as_uri()
    else:
        arguments["base_branch"] = base

    result = await registry.call("create_clone", arguments)

    assert result["success"] is True, result
    expected = {"main": main, "origin/main": remote_tip, "external": remote_tip}[base]
    assert git(path, "rev-parse", "HEAD") == expected
    assert result["base_commit_sha"] == expected
    assert result["index_source"] == ("cold" if base == "external" else "local")
    if base != "external":
        assert storage.create.call_args.kwargs["base_branch"] == "main"
        assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "child"
    assert git(repo, "rev-parse", "refs/heads/main") == main


async def test_extracted_merge_path(repo: Path) -> None:
    from gobby.clones import merge

    assert len(Path(clone_git.__file__).read_text().splitlines()) < 1000
    assert len(Path(merge.__file__).read_text().splitlines()) < 1000
    git(repo, "switch", "-c", "work")
    git(repo, "switch", "-c", "feature", "main")
    feature = commit(repo, "feature.txt", "feature\n", "feature")
    git(repo, "switch", "-c", "conflict", "main")
    commit(repo, "tracked.txt", "conflict side\n", "conflict side")
    git(repo, "switch", "main")
    commit(repo, "tracked.txt", "main side\n", "main side")
    git(repo, "push", "origin", "main")
    git(repo, "switch", "work")
    manager = CloneGitManager(repo)

    merged = await manager.merge_branch("feature", "main", source_is_local=True)

    assert merged.success is True, merged.message
    assert git(repo, "merge-base", "--is-ancestor", feature, "main") == ""
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work"

    conflicted = await merge.merge_clone_branch(manager, "conflict", "main", source_is_local=True)

    assert conflicted.success is False
    assert conflicted.error == "merge_conflict"
    assert conflicted.output == "tracked.txt"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work"
