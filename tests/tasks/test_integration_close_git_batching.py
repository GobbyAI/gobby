"""Integration-sized linked sets retain close semantics without pairwise Git calls."""

import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.tasks._task_scope import collect_net_commit_paths_async
from gobby.tasks.commits import collect_commit_diff_text_async
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Gobby Tests", "-c", "user.email=tests@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "--no-gpg-sign", "-qm", path)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "feature.py", "VALUE = 0\n")
    return tmp_path


def _linked_history(repo: Path, count: int) -> list[str]:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "source")
    feature = _commit(repo, "feature.py", "VALUE = 1\n")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "before.py", "FOREIGN = True\n")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-qm", "land source", "source")
    landing = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "target", base)
    _commit(repo, "incoming.py", "FOREIGN = True\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "--no-gpg-sign", "-qm", "sync target", "target")
    sync = _git(repo, "rev-parse", "HEAD")
    linked = [feature, landing, sync]
    for index in range(count - 4):
        _git(repo, "commit", "--no-gpg-sign", "--allow-empty", "-qm", f"checkpoint {index}")
        linked.append(_git(repo, "rev-parse", "HEAD"))
    linked.append(_commit(repo, "feature.py", "VALUE = 2\n"))
    return linked


@pytest.mark.parametrize("count", [38, 201])
@pytest.mark.asyncio
async def test_many_linked_commits_use_bounded_git_work(
    repo: Path, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = _linked_history(repo, count)
    original = daemon_git.run
    calls: list[tuple[str, ...]] = []

    async def counted(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitOk | GitFailed | GitTimeout:
        calls.append(tuple(args))
        return await original(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

    monkeypatch.setattr(daemon_git, "run", counted)
    paths = await collect_net_commit_paths_async(linked, str(repo), candidate=linked[-1])

    assert paths.changed == frozenset({"feature.py"})
    assert paths.deleted == frozenset()
    assert paths.undelivered == ()
    counts = Counter(command[0] for command in calls)
    assert len(calls) <= 2 * count + 20, counts
    assert counts["merge-base"] <= 8, counts
    print(f"{count} linked commits: {len(calls)} Git calls; {dict(counts)}")

    diff = await collect_commit_diff_text_async(linked, cwd=repo)
    assert "+VALUE = 2" in diff
    assert "+VALUE = 1" not in diff
    assert "before.py" not in diff
    assert "incoming.py" not in diff


@pytest.mark.asyncio
async def test_undelivered_short_and_duplicate_links_keep_their_identity(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    delivered = _commit(repo, "feature.py", "VALUE = 1\n")
    _git(repo, "checkout", "-qb", "orphan", base)
    orphan = _commit(repo, "orphan.py", "UNDELIVERED = True\n")

    paths = await collect_net_commit_paths_async(
        [delivered[:10], orphan[:10], delivered, orphan[:10]],
        str(repo),
        candidate=delivered[:10],
    )

    assert paths.changed == frozenset({"feature.py"})
    assert paths.deleted == frozenset()
    assert paths.undelivered == (orphan[:10], orphan[:10])


@pytest.mark.parametrize("invalid", ["missing-commit", "HEAD:feature.py"])
@pytest.mark.asyncio
async def test_invalid_link_still_fails_delivery_checks(repo: Path, invalid: str) -> None:
    candidate = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="Cannot check whether the close candidate delivers"):
        await collect_net_commit_paths_async([candidate, invalid], str(repo), candidate=candidate)
