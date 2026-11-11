"""A literal `git apply` runs the patch bytes Gobby validated, not a later read of the file."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks import checkout_reads
from gobby.hooks._normalization_apply_rewrite import MAX_PATCH_BYTES
from gobby.hooks._normalization_tools import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit

_PATCH = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
_SWAPPED = _PATCH.replace("+two", "+evil")
_REWRITE = "_validated_apply_command"
_READS = "canonical_read_file_paths"
_SECRET = "SECRET-BYTES-OUTSIDE-THE-TREE"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "dev@example.com"],
        ["config", "user.name", "Dev"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _bash(repo: Path, command: str, *, can_rewrite_input: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(repo)},
        "project_path": str(repo),
    }
    _set_canonical_tool_metadata(data, can_rewrite_input=can_rewrite_input)
    return data


def _command_that_runs(data: dict[str, Any]) -> str:
    """The command the CLI runs: the validated rewrite when one was built."""
    command: str = data.get(_REWRITE, data["tool_input"]["command"])
    return command


def test_swapping_the_patch_after_validation_applies_the_validated_bytes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    patch = repo / "workspace" / "set-aside.patch"
    patch.parent.mkdir()
    patch.write_text(_PATCH, encoding="utf-8")

    data = _bash(repo, f"git apply {patch}")
    # A sibling rewrites the file after validation and before the CLI runs it.
    patch.write_text(_SWAPPED, encoding="utf-8")

    subprocess.run(["bash", "-c", _command_that_runs(data)], cwd=repo, check=True)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_validated_bytes_reach_only_the_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        data = _bash(repo, f"git apply {patch}")

    assert "+two" in data[_REWRITE]
    for key, value in data.items():
        if key != _REWRITE:
            assert "+two" not in repr(value), key
    assert "+two" not in caplog.text


def test_a_patch_line_equal_to_the_delimiter_refuses_the_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    # Text before the first diff header is ignored by git apply, and this line would
    # close the quoted heredoc the rewrite opens with the same delimiter.
    patch.write_text("ABCDEF\n" + _PATCH, encoding="utf-8")
    monkeypatch.setattr("gobby.hooks._normalization_apply_rewrite.new_delimiter", lambda: "ABCDEF")

    data = _bash(repo, f"git apply {patch}")

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("stdin", id="stdin"),
        pytest.param("two-operands", id="two-operands"),
        pytest.param("option-terminator", id="option-terminator"),
        pytest.param("quoted", id="quoted"),
        pytest.param("dynamic", id="dynamic"),
        pytest.param("compound", id="compound"),
    ],
)
def test_refused_shapes_never_read_the_patch_or_rewrite(tmp_path: Path, shape: str) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")
    command = {
        "stdin": "git apply -",
        "two-operands": f"git apply {patch} {patch}",
        "option-terminator": f"git apply -- {patch}",
        "quoted": f'git apply "{patch}"',
        "dynamic": "git apply $PATCH",
        "compound": f"git apply {patch} && true",
    }[shape]

    data = _bash(repo, command)

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_literal_cached_apply_with_a_validated_patch_stays_rewritten(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")

    data = _bash(repo, f"git apply --cached {patch}")

    assert data["canonical_tool_kind"] == "execute"
    assert "+two" in data[_REWRITE]


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("two-operands", id="two-operands"),
        pytest.param("quoted", id="quoted"),
        pytest.param("dynamic", id="dynamic"),
        pytest.param("compound", id="compound"),
    ],
)
def test_refused_cached_shapes_report_unknown_scope(tmp_path: Path, shape: str) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")
    command = {
        "two-operands": f"git apply --cached {patch} {patch}",
        "quoted": f'git apply --cached "{patch}"',
        "dynamic": "git apply --cached $PATCH",
        "compound": f"git apply --cached {patch} && true",
    }[shape]

    data = _bash(repo, command)

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_cached_apply_over_the_cap_reports_unknown_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_bytes(b"x" * (MAX_PATCH_BYTES + 1))

    data = _bash(repo, f"git apply --cached {patch}")

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_cached_apply_for_a_provider_that_cannot_rewrite_reports_unknown_scope(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")

    data = _bash(repo, f"git apply --cached {patch}", can_rewrite_input=False)

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("content", "id_"),
    [
        pytest.param(_PATCH.encode("utf-8") + b"\xff\n", "non-utf8", id="non-utf8"),
        pytest.param(_PATCH.encode("utf-8") + b"\x00\n", "nul", id="nul"),
        pytest.param(_PATCH.rstrip("\n").encode("utf-8"), "no-newline", id="no-newline"),
        pytest.param(b"x" * (MAX_PATCH_BYTES + 1), "over-cap", id="over-cap"),
    ],
)
def test_invalid_patch_bytes_are_refused_without_a_rewrite(
    tmp_path: Path, content: bytes, id_: str
) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_bytes(content)

    data = _bash(repo, f"git apply {patch}")

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "worktree"])
@pytest.mark.parametrize(
    "shape", ["outside", "symlink-file", "symlink-dir", "dotdot", "hardlink", "fifo"]
)
def test_a_patch_outside_the_checkout_is_refused_unread(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, shape: str, cached: bool
) -> None:
    # The hook reads outside the seat sandbox and returns the bytes to the CLI, so a
    # patch that does not resolve inside the checkout must never be read or embedded.
    repo = _repo(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text(_SECRET + "\n" + _PATCH, encoding="utf-8")
    if shape == "symlink-file":
        (repo / "link.patch").symlink_to(secret)
    elif shape == "symlink-dir":
        (repo / "linked").symlink_to(tmp_path)
    elif shape == "hardlink":
        (repo / "hard.patch").hardlink_to(secret)
    elif shape == "fifo":
        os.mkfifo(repo / "pipe.patch")
    operand = {
        "outside": str(secret),
        "symlink-file": str(repo / "link.patch"),
        "symlink-dir": str(repo / "linked" / "secret.txt"),
        "dotdot": "../secret.txt",
        "hardlink": str(repo / "hard.patch"),
        "fifo": str(repo / "pipe.patch"),
    }[shape]
    flags = "--cached " if cached else ""

    with caplog.at_level(logging.DEBUG):
        data = _bash(repo, f"git apply {flags}{operand}")

    assert _REWRITE not in data
    assert _READS not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert _SECRET not in repr(data)
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "worktree"])
def test_a_node_origin_apply_never_reads_the_hub_file_at_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    # A node reports its own checkout root; a hub file at the same path is not the node's
    # patch, so its bytes must never reach the node's CLI.
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_SECRET + "\n" + _PATCH, encoding="utf-8")
    monkeypatch.setattr(checkout_reads, "node_origin", lambda: True)
    flags = "--cached " if cached else ""

    data = _bash(repo, f"git apply {flags}{patch}")

    assert _REWRITE not in data
    assert _READS not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert _SECRET not in repr(data)


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "worktree"])
def test_an_in_tree_patch_reports_its_read_path(tmp_path: Path, cached: bool) -> None:
    repo = _repo(tmp_path)
    (repo / "patches").mkdir()
    patch = repo / "patches" / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")
    flags = "--cached " if cached else ""

    data = _bash(repo, f"git apply {flags}patches/set-aside.patch")

    assert "+two" in data[_REWRITE]
    assert data[_READS] == [str(patch)]
    subprocess.run(["bash", "-c", _command_that_runs(data)], cwd=repo, check=True)
    if cached:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert staged == "a.txt\n"
    else:
        assert (repo / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_provider_that_cannot_rewrite_input_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = repo / "set-aside.patch"
    patch.write_text(_PATCH, encoding="utf-8")

    data = _bash(repo, f"git apply {patch}", can_rewrite_input=False)

    assert _REWRITE not in data
    assert data["canonical_repo_mutation_scope_unknown"] is True
