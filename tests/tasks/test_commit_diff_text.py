"""Net-patch assembly for the close criteria review (#21037)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.tasks.commits import (
    collect_commit_diff_text,
    collect_commit_diff_text_async,
    collect_commit_rename_aliases_async,
    collect_net_name_status_async,
    resolve_task_tagged_commits_async,
)

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


@pytest.mark.parametrize("linked_first", [True, False])
@pytest.mark.parametrize("linked_second", [True, False])
@pytest.mark.asyncio
async def test_sync_merge_ignores_unrelated_linked_history(
    repo: Path, linked_first: bool, linked_second: bool
) -> None:
    initial = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "historical")
    historical = _commit(repo, "historical.py", "HISTORICAL = True\n", "old task revision")
    _git(repo, "checkout", "-q", "-b", "task", initial)
    first = _commit(repo, "feature.py", "FEATURE = True\n", "[gobby-#42] task feature")
    _git(repo, "checkout", "-q", "main")
    second = _commit(repo, "incoming.py", "INCOMING = True\n", "target unrelated work")
    _git(repo, "checkout", "-q", "task")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", "[gobby-#42] sync target", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    linked = [historical, sync]
    if linked_first:
        manager = MagicMock()
        manager.get_task.return_value = MagicMock(id="task-uuid", seq_num=42)
        with patch("gobby.tasks.commits._resolve_branch_for_task", return_value=None):
            rediscovered = await resolve_task_tagged_commits_async(
                manager,
                task_id="task-uuid",
                since="2026-01-01T00:00:00Z",
                cwd=repo,
                project_name="gobby",
            )
        assert {_git(repo, "rev-parse", sha) for sha in rediscovered} == {first, sync}
        manager.link_commit.assert_not_called()
        linked = [historical, *rediscovered]
    if linked_second:
        linked.append(second)

    diff = await collect_commit_diff_text_async(linked, cwd=repo)

    assert "HISTORICAL = True" in diff
    assert ("incoming.py" in diff) is (not linked_first or linked_second)
    if linked_first:
        assert "FEATURE = True" in diff
    if linked_first and not linked_second:
        paths = _paths(await collect_net_name_status_async(linked, cwd=repo))
        assert set(paths) == {"historical.py", "feature.py"}


@pytest.mark.asyncio
async def test_linked_renames_follow_history_but_not_copies_or_new_files(repo: Path) -> None:
    (repo / "tests").mkdir()
    first = _commit(
        repo, "tests/old_test_feature.py", "def test_feature():\n    assert 1\n", "test"
    )
    _git(repo, "mv", "tests/old_test_feature.py", "tests/mid_test_feature.py")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "rename once")
    second = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "tests/mid_test_feature.py", "tests/new_test_feature.py")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "rename twice")
    third = _git(repo, "rev-parse", "HEAD")
    copied = _commit(
        repo, "tests/copied_test_feature.py", "def test_feature():\n    assert 1\n", "copy"
    )
    added = _commit(repo, "tests/added_test_feature.py", "def test_new():\n    assert 1\n", "new")

    aliases = await collect_commit_rename_aliases_async(
        [added, third, copied, second, first], cwd=repo
    )

    assert aliases["tests/new_test_feature.py"] == (
        "tests/old_test_feature.py",
        "tests/mid_test_feature.py",
    )
    assert "tests/copied_test_feature.py" not in aliases
    assert "tests/added_test_feature.py" not in aliases


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


@pytest.mark.parametrize("historical_link", [False, True])
def test_conflict_sync_merge_contributes_only_its_resolution(
    repo: Path, historical_link: bool
) -> None:
    """A resolved sync merge contributes its remerge diff, not the incoming branch."""
    _commit(repo, "shared.py", "BASE = True\n", "base shared")
    initial = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "old-task")
    historical = _commit(repo, "historical.py", "HISTORICAL = True\n", "old task revision")
    _git(repo, "checkout", "-q", "main")
    assert _git(repo, "rev-parse", "HEAD") == initial
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

    linked = [sync, _git(repo, "rev-parse", "HEAD")]
    if historical_link:
        linked.append(historical)
    diff = collect_commit_diff_text(linked, cwd=repo)

    assert "unrelated.py" not in diff
    assert "remerge CONFLICT" in diff
    assert "+RESOLVED = True" in diff
    assert "MARKER = True" in diff


def _merge(repo: Path, branch: str, message: str) -> str:
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-q", "-m", message, branch)
    return _git(repo, "rev-parse", "HEAD")


def _branch_commit(repo: Path, branch: str, start: str, path: str, content: str) -> str:
    _git(repo, "checkout", "-q", "-b", branch, start)
    return _commit(repo, path, content, branch)


def _paths(name_status: str | None) -> list[str]:
    assert name_status is not None
    fields = [field for field in name_status.split("\0") if field]
    return fields[1::2]


async def test_managed_integration_nets_only_its_linked_merges(repo: Path) -> None:
    """#23302's shape: managed merges of a private branch and of an integration
    branch holding an inner merge of two landed branches, with an unlinked foreign
    commit on shared's first-parent chain between them (#23314)."""
    private = _branch_commit(repo, "private", "main", "a.py", "A = 1\n")
    _git(repo, "checkout", "-q", "main")
    checkpoint_a = _merge(repo, "private", "managed checkpoint A")
    foreign = _commit(repo, "foreign.py", "FOREIGN = True\n", "unlinked foreign commit")
    landed_x = _branch_commit(repo, "x", foreign, "x.py", "X = 1\n")
    landed_y = _branch_commit(repo, "y", foreign, "y.py", "Y = 1\n")
    _git(repo, "checkout", "-q", "-b", "package", foreign)
    inner_x = _merge(repo, "x", "inner merge x")
    inner_y = _merge(repo, "y", "inner merge y")
    _git(repo, "checkout", "-q", "main")
    checkpoint_b = _merge(repo, "package", "managed checkpoint B")
    linked = [private, checkpoint_a, landed_x, landed_y, inner_x, inner_y, checkpoint_b]

    listing = await collect_net_name_status_async(linked, cwd=repo)
    diff = await collect_commit_diff_text_async(linked, cwd=repo)

    assert sorted(_paths(listing)) == ["a.py", "x.py", "y.py"]
    assert "foreign.py" not in diff
    for path in ("a.py", "x.py", "y.py"):
        assert diff.count(f"diff --git a/{path} b/{path}") == 1


async def test_linked_merge_and_its_second_parent_commits_apply_once(repo: Path) -> None:
    """A merge's first-parent diff already carries its linked second-parent commits."""
    first = _branch_commit(repo, "side", "main", "probe.py", "def probe():\n    return 1\n")
    second = _commit(repo, "probe.py", "def probe():\n    return 2\n", "side second")
    _git(repo, "checkout", "-q", "main")
    landing = _merge(repo, "side", "land side")
    follow = _commit(repo, "probe.py", "def probe():\n    return 3\n", "main follow")

    listing = await collect_net_name_status_async([first, second, landing, follow], cwd=repo)
    diff = await collect_commit_diff_text_async([first, second, landing, follow], cwd=repo)

    assert _paths(listing) == ["probe.py"]
    assert diff.count("diff --git a/probe.py b/probe.py") == 1
    assert "+    return 3" in diff
    assert "return 2" not in diff


async def test_merge_that_needs_unlinked_first_parent_content_stays_unavailable(
    repo: Path,
) -> None:
    """A merge whose diff builds on an unlinked commit cannot be netted from the base."""
    started = _branch_commit(repo, "early", "main", "early.py", "EARLY = True\n")
    _git(repo, "checkout", "-q", "main")
    early = _merge(repo, "early", "land early")
    foreign = _commit(repo, "probe.py", "def probe():\n    return 5\n", "unlinked foreign")
    change = _branch_commit(repo, "late", foreign, "probe.py", "def probe():\n    return 6\n")
    _git(repo, "checkout", "-q", "main")
    late = _merge(repo, "late", "land late")

    linked = [started, early, change, late]

    assert await collect_net_name_status_async(linked, cwd=repo) is None


async def test_conflicted_sync_inside_a_linked_merge_applies_once(repo: Path) -> None:
    """A sync merge reached only through a linked merge's second parent arrives
    with that merge's first-parent diff, so its resolution is not appended again."""
    _commit(repo, "f.py", "BASE = True\n", "base f")
    _branch_commit(repo, "q", "main", "f.py", "Q = True\n")
    branch_work = _branch_commit(repo, "p", "main", "f.py", "P = True\n")
    conflict = subprocess.run(
        ["git", *_GIT_IDENTITY, "merge", "--no-ff", "--no-gpg-sign", "-m", "sync q", "q"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert conflict.returncode != 0
    (repo / "f.py").write_text("RESOLVED = True\n")
    _git(repo, "add", "f.py")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "sync q")
    sync = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    landing = _merge(repo, "p", "land p")
    follow = _commit(repo, "g.py", "G = True\n", "follow-up")
    linked = [branch_work, sync, landing, follow]

    listing = await collect_net_name_status_async(linked, cwd=repo)
    diff = await collect_commit_diff_text_async(linked, cwd=repo)

    assert _paths(listing) == ["f.py", "g.py"]
    assert diff.count("diff --git a/f.py b/f.py") == 1
    assert diff.count("+RESOLVED = True") == 1
