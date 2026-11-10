"""Clearing Gobby-generated untracked paths never reaches outside the worktree (#25840).

Git status names the generated paths first; the tree can change before the daemon clears
them. A parent directory swapped for a symlink after that status must not redirect the
unlink or rmtree to the symlink's target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.worktrees.deletion import _clear_generated_untracked

pytestmark = pytest.mark.unit


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("operator data")
    (outside / "keep-dir").mkdir()
    (outside / "keep-dir" / "nested.txt").write_text("operator data")
    return outside


def test_a_parent_swapped_for_a_symlink_does_not_redirect_a_file_unlink(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = _outside(tmp_path)
    (tree / "gen").symlink_to(outside)

    error = _clear_generated_untracked(tree, ["gen/sub/keep.txt"])

    assert error is not None
    assert "gen/sub/keep.txt" in error
    assert (outside / "sub" / "keep.txt").read_text() == "operator data"


def test_a_parent_swapped_for_a_symlink_does_not_redirect_an_rmtree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = _outside(tmp_path)
    (tree / "gen").symlink_to(outside)

    error = _clear_generated_untracked(tree, ["gen/keep-dir"])

    assert error is not None
    assert (outside / "keep-dir" / "nested.txt").read_text() == "operator data"


def test_a_deeper_parent_symlink_is_refused_too(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "gen").mkdir(parents=True)
    outside = _outside(tmp_path)
    (tree / "gen" / "cache").symlink_to(outside)

    error = _clear_generated_untracked(tree, ["gen/cache/sub/keep.txt"])

    assert error is not None
    assert (outside / "sub" / "keep.txt").exists()


def test_a_final_symlink_is_unlinked_and_its_target_kept(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = _outside(tmp_path)
    (tree / "link").symlink_to(outside / "keep-dir")

    assert _clear_generated_untracked(tree, ["link"]) is None

    assert not (tree / "link").is_symlink()
    assert (outside / "keep-dir" / "nested.txt").exists()


def test_generated_files_and_directories_inside_the_tree_are_cleared(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "gen" / "deep").mkdir(parents=True)
    (tree / "gen" / "deep" / "a.txt").write_text("x")
    (tree / "gen" / "b.txt").write_text("y")
    (tree / "top.log").write_text("z")

    assert _clear_generated_untracked(tree, ["gen/deep", "gen/b.txt", "top.log"]) is None

    assert sorted(p.name for p in tree.rglob("*")) == ["gen"]


def test_an_already_missing_path_is_not_an_error(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()

    assert _clear_generated_untracked(tree, ["gone/file.txt", "absent.txt"]) is None


def test_paths_escaping_the_tree_are_refused(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()

    error = _clear_generated_untracked(tree, ["../outside"])

    assert error is not None and "outside the worktree" in error
