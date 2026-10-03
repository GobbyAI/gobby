"""#23280 item 5: a session binds to the worktree its workspace resolves into."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.servers.routes.sessions import core
from gobby.worktrees.containment import containing_worktree_id, path_is_within, worktree_roots

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Worktree:
    id: str
    worktree_path: str


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """`wt` and its nested `wt/inner`, a sibling `outside`, and three links:
    `wt/escape` -> `outside`, `alias` -> `wt`, `wt/escape/..` resolves to tmp."""
    (tmp_path / "wt" / "inner" / "src").mkdir(parents=True)
    (tmp_path / "outside" / "src").mkdir(parents=True)
    (tmp_path / "wt" / "escape").symlink_to(tmp_path / "outside")
    (tmp_path / "alias").symlink_to(tmp_path / "wt")
    return tmp_path


def _worktrees(root: Path) -> list[_Worktree]:
    return [
        _Worktree("wt", str(root / "wt")),
        _Worktree("wt-inner", str(root / "wt" / "inner")),
        _Worktree("empty", ""),
    ]


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        ("wt/src", "wt"),
        ("wt/inner/src", "wt-inner"),
        ("wt/escape/src", None),
        ("wt/escape/..", None),
        ("alias/inner/src", "wt-inner"),
        ("outside/src", None),
        ("wt/missing/../src", "wt"),
    ],
    ids=[
        "descendant",
        "deepest",
        "symlink-escape",
        "symlink-dotdot",
        "alias-in",
        "outside",
        "missing",
    ],
)
def test_workspace_binds_the_worktree_it_resolves_into(
    tree: Path, workspace: str, expected: str | None
) -> None:
    roots = worktree_roots(_worktrees(tree))
    assert containing_worktree_id(str(tree / workspace), roots) == expected


def test_path_is_within_follows_symlinks(tree: Path) -> None:
    assert path_is_within(str(tree / "alias" / "inner"), str(tree / "wt"))
    assert not path_is_within(str(tree / "wt" / "escape" / "src"), str(tree / "wt"))


def test_only_this_machines_sessions_bind(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.utils.machine_id.get_machine_id", lambda: "local")
    listed: list[str] = []

    def list_worktrees(project_id: str) -> list[_Worktree]:
        listed.append(project_id)
        return _worktrees(tree)

    server: Any = SimpleNamespace(
        services=SimpleNamespace(worktree_storage=SimpleNamespace(list_worktrees=list_worktrees))
    )
    rows: list[dict[str, Any]] = [
        {"project_id": "p", "machine_id": "local", "workspace_path": str(tree / "wt" / "src")},
        {"project_id": "p", "machine_id": "local", "workspace_path": str(tree / "wt" / "escape")},
        {"project_id": "p", "machine_id": "remote", "workspace_path": str(tree / "wt" / "src")},
        {"project_id": "p", "machine_id": "local", "workspace_path": None},
    ]

    core._bind_worktrees(server, rows)

    assert [row["worktree_id"] for row in rows] == ["wt", None, None, None]
    assert listed == ["p"], "one worktree listing per project"


def test_an_unreadable_machine_id_binds_nothing(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> str:
        raise OSError("machine_id unreadable")

    monkeypatch.setattr("gobby.utils.machine_id.get_machine_id", unreadable)
    server: Any = SimpleNamespace(
        services=SimpleNamespace(
            worktree_storage=SimpleNamespace(list_worktrees=lambda project_id: _worktrees(tree))
        )
    )
    rows: list[dict[str, Any]] = [
        {"project_id": "p", "machine_id": "local", "workspace_path": str(tree / "wt" / "src")}
    ]

    core._bind_worktrees(server, rows)

    assert rows[0]["worktree_id"] is None
