"""Net-patch assembly for the close criteria review (#21037)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.tasks.commits import collect_commit_diff_text

pytestmark = pytest.mark.unit

_GIT_IDENTITY = ["-c", "user.name=Gobby Tests", "-c", "user.email=gobby-tests@example.com"]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _show(repo: Path, sha: str) -> str:
    return _git(repo, "show", "--format=", "--find-renames", "--find-copies", "--binary", sha)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "probe.py", "def probe():\n    return get(url, timeout=3)\n", "initial")
    return path


def test_empty_commit_set_has_no_patch() -> None:
    assert collect_commit_diff_text([], cwd=".") == ""


def test_single_commit_returns_exactly_its_patch(repo: Path) -> None:
    sha = _commit(
        repo,
        "probe.py",
        "def probe():\n    return get(url, headers=auth(), timeout=3)\n",
        "authenticate",
    )

    assert collect_commit_diff_text([sha], cwd=repo) == _show(repo, sha)


def test_net_patch_preserves_crlf_bytes(repo: Path) -> None:
    path = repo / "crlf.txt"
    path.write_bytes(b"first\r\nsecond\r\n")
    _git(repo, "add", path.name)
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "add crlf")
    sha = _git(repo, "rev-parse", "HEAD")

    diff = collect_commit_diff_text([sha], cwd=repo)

    assert "+first\r\n+second" in diff


def test_later_commit_supersedes_an_earlier_hunk(repo: Path) -> None:
    """The reviewer sees the code as it stands after every linked commit, not each step."""
    first = _commit(repo, "gate.py", "def fetch():\n    return get(url, timeout=3)\n", "add gate")
    second = _commit(
        repo,
        "gate.py",
        "def fetch():\n    return get(url, headers=auth(), timeout=3)\n",
        "authenticate gate",
    )

    # Order-insensitive: close_task links commits in the order they were attached.
    diff = collect_commit_diff_text([second, first], cwd=repo)

    assert diff.count("diff --git a/gate.py b/gate.py") == 1
    assert "+    return get(url, headers=auth(), timeout=3)" in diff
    assert "+    return get(url, timeout=3)" not in diff
    assert "-    return get(url, timeout=3)" not in diff


def test_net_patch_keeps_only_the_linked_commits_hunks(repo: Path) -> None:
    """A foreign commit between two linked ones touching another file is not evidence."""
    first = _commit(repo, "gate.py", "def fetch():\n    return 1\n", "add gate")
    _commit(repo, "other.py", "OTHER = 1\n", "foreign work")
    second = _commit(repo, "gate.py", "def fetch():\n    return 2\n", "rewrite gate")

    diff = collect_commit_diff_text([first, second], cwd=repo)

    assert "other.py" not in diff
    assert diff.count("diff --git") == 1
    assert "+    return 2" in diff
    assert "+    return 1" not in diff


def test_root_commit_diffs_against_the_empty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "root"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    sha = _commit(repo, "a.txt", "hello\n", "root")

    diff = collect_commit_diff_text([sha], cwd=repo)

    assert "new file mode" in diff
    assert "+hello" in diff
    assert diff == _show(repo, sha)


def test_unreplayable_set_falls_back_to_the_per_commit_stream(repo: Path) -> None:
    """Two commits rewriting the same line on different branches cannot share a base."""
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "probe.py", "def probe():\n    return get(url, timeout=9)\n", "side")
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "probe.py", "def probe():\n    return get(url, timeout=5)\n", "main")

    diff = collect_commit_diff_text([side, main], cwd=repo)

    assert diff.count("diff --git a/probe.py b/probe.py") == 2
    assert "+    return get(url, timeout=9)" in diff
    assert "+    return get(url, timeout=5)" in diff


def test_unresolvable_commit_raises(repo: Path) -> None:
    with pytest.raises(RuntimeError, match="git show failed"):
        collect_commit_diff_text(["0" * 40], cwd=repo)


def test_landing_merge_nets_to_its_first_parent_diff(repo: Path) -> None:
    """A merge that lands the linked commits diffs against the branch it landed on.

    Mirrors an epic landing: the branch syncs the target in, fixes, then lands.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "feature.py", "def feature():\n    return 1\n", "side feature")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "unrelated.py", "UNRELATED = True\n", "main unrelated")
    _git(repo, "checkout", "-q", "side")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", "sync main", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    fix = _commit(repo, "feature.py", "def feature():\n    return 2\n", "side fix")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", "land side", "side")
    landing = _git(repo, "rev-parse", "HEAD")

    diff = collect_commit_diff_text([sync, fix, landing], cwd=repo)

    assert diff.count("diff --git a/feature.py b/feature.py") == 1
    assert "+    return 2" in diff
    assert "return 1" not in diff
    assert "unrelated.py" not in diff


def test_a_lone_landing_merge_carries_the_branch_it_landed(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "feature.py", "def feature():\n    return 1\n", "side feature")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", "land side", "side")
    landing = _git(repo, "rev-parse", "HEAD")

    diff = collect_commit_diff_text([landing], cwd=repo)

    assert "diff --git a/feature.py b/feature.py" in diff
    assert "+    return 1" in diff


def test_clean_sync_merge_contributes_no_hunks(repo: Path) -> None:
    """Syncing the target into the task branch adds nothing the merge did not author."""
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "feature.py", "def feature():\n    return 1\n", "side feature")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "unrelated.py", "UNRELATED = True\n", "main unrelated")
    _git(repo, "checkout", "-q", "side")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", "sync main", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    fix = _commit(repo, "feature.py", "def feature():\n    return 2\n", "side fix")

    diff = collect_commit_diff_text([sync, fix], cwd=repo)

    assert "unrelated.py" not in diff
    assert diff.count("diff --git a/feature.py b/feature.py") == 1
    assert "+    return 2" in diff


def test_conflict_sync_merge_contributes_only_its_resolution(repo: Path) -> None:
    """A resolved sync merge contributes its remerge diff, not the incoming branch."""
    _commit(repo, "shared.py", "BASE = True\n", "base shared")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "shared.py", "SIDE = True\n", "side shared")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "shared.py", "MAIN = True\n", "main shared")
    _commit(repo, "unrelated.py", "UNRELATED = True\n", "main unrelated")
    _git(repo, "checkout", "-q", "side")
    conflict = subprocess.run(
        ["git", *_GIT_IDENTITY, "merge", "--no-ff", "--no-gpg-sign", "-m", "sync main", "main"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert conflict.returncode != 0
    (repo / "shared.py").write_text("RESOLVED = True\n")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "sync main")
    sync = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "marker.py", "MARKER = True\n", "side follow")

    diff = collect_commit_diff_text([sync, _git(repo, "rev-parse", "HEAD")], cwd=repo)

    assert "unrelated.py" not in diff
    assert "remerge CONFLICT" in diff
    assert "+RESOLVED = True" in diff
    assert "MARKER = True" in diff
