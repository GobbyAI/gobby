"""The reference-transaction guard keeps code off the main checkout's branch.

Each test installs the rendered template into a temporary repository and drives
real git with a hermetic environment that never carries the operator override.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from gobby.cli.installers.git_hooks import GOBBY_HOOK_END, HOOK_TEMPLATES, install_git_hooks
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.close_receipts import INDEPENDENT_REVIEW_APPROVAL, record_close_receipt
from gobby.tasks.land_commit import land_candidate
from gobby.tasks.landing_policy import is_direct_commit_path
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

LAND_COMMIT_TEXT = "gobby-tasks-ops:land_commit"
BASE_PLAN = ".gobby/plans/p.md"
BASE_MANIFEST = ".gobby/plans/coverage/p/1/p.coverage.yaml"

# Paths compared one commit at a time against is_direct_commit_path.
ALLOWED_PATHS = {
    "docs/x.md",
    "docs/sub/deep.md",
    "NOTES.md",
    ".gobby/plans/q.md",
    ".gobby/roles/r.md",
    ".gobby/plans/coverage/q/2/q.coverage.yaml",
    ".gobby/plans/coverage/.regenerate.log",
}
REFUSED_PATHS = {
    "src/gobby/a.py",
    "src/gobby/AGENTS.md",
    "src/gobby/install/shared/skills/s/SKILL.md",
    "docs/reference-audit/a.json",
    ".gobby/plans/x.svg",
    ".gobby/roles/r.yaml",
    "README.txt",
}


class _Repo:
    """A temporary repository whose reference-transaction hook is the guard."""

    def __init__(self, tmp_path: Path, *init_args: str) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        git = shutil.which("git")
        assert git is not None
        (bin_dir / "git").symlink_to(git)
        (tmp_path / "home").mkdir()
        self.env = {
            "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(tmp_path / "home"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@gobby.local",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@gobby.local",
        }
        # Command prefix for every git call, such as a sandbox.
        self.wrapper: list[str] = []
        self.root = tmp_path / "repo"
        self.ok(tmp_path, "init", "-q", "-b", "main", *init_args, str(self.root))
        hook = self.root / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\n{HOOK_TEMPLATES['reference-transaction']}", encoding="utf-8")
        hook.chmod(0o755)

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.wrapper, "git", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def ok(self, cwd: Path, *args: str) -> str:
        result = self.git(cwd, *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def commit(self, cwd: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Write and stage exactly ``files``, then commit them."""
        for rel, content in files.items():
            (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
            (cwd / rel).write_text(content, encoding="utf-8")
        self.ok(cwd, "add", "--", *files)
        return self.git(cwd, "commit", "-q", "-m", "change")

    def unstage(self, cwd: Path, *paths: str) -> None:
        self.ok(cwd, "reset", "-q", "--", *paths)


def _baseline(tmp_path: Path, *init_args: str) -> _Repo:
    repo = _Repo(tmp_path, *init_args)
    # The first commit creates the protected ref, which the guard allows even for code.
    created = repo.commit(
        repo.root, {"src/gobby/base.py": "x = 1\n", BASE_PLAN: "plan\n", BASE_MANIFEST: "m\n"}
    )
    assert created.returncode == 0, created.stderr
    return repo


def _assert_refused(result: subprocess.CompletedProcess[str], path: str) -> None:
    assert result.returncode != 0
    assert f"{path} is not a direct-commit path" in result.stderr
    assert LAND_COMMIT_TEXT in result.stderr


def test_direct_commit_allows_listed_markdown_and_coverage(tmp_path: Path) -> None:
    repo = _baseline(tmp_path)

    docs = repo.commit(repo.root, {"docs/x.md": "x\n", "NOTES.md": "n\n"})
    coverage = repo.commit(
        repo.root,
        {
            ".gobby/plans/coverage/p/2/p.coverage.yaml": "m\n",
            ".gobby/plans/coverage/.regenerate.log": "log\n",
        },
    )
    repo.ok(repo.root, "rm", "-q", BASE_MANIFEST)
    (repo.root / ".gobby/plans/completed").mkdir()
    repo.ok(repo.root, "mv", BASE_PLAN, ".gobby/plans/completed/p.md")
    archive = repo.git(repo.root, "commit", "-q", "-m", "archive")

    assert docs.returncode == 0, docs.stderr
    assert coverage.returncode == 0, coverage.stderr
    assert archive.returncode == 0, archive.stderr

    verdicts: dict[str, bool] = {}
    for path in sorted(ALLOWED_PATHS | REFUSED_PATHS):
        result = repo.commit(repo.root, {path: "change\n"})
        verdicts[path] = result.returncode == 0
        if not verdicts[path]:
            _assert_refused(result, path)
            repo.unstage(repo.root, path)

    assert {path for path, allowed in verdicts.items() if allowed} == ALLOWED_PATHS
    assert verdicts == {path: is_direct_commit_path(path) for path in verdicts}


def test_protected_branch_refuses_writes_from_any_worktree(tmp_path: Path) -> None:
    repo = _baseline(tmp_path)
    lane = tmp_path / "lane"
    main_view = tmp_path / "main-view"
    repo.ok(repo.root, "worktree", "add", "-q", "-b", "lane", str(lane))
    lane_commit = repo.commit(lane, {"src/gobby/a.py": "a = 1\n"})
    repo.ok(repo.root, "worktree", "add", "-q", "--force", str(main_view), "main")
    main_before = repo.ok(repo.root, "rev-parse", "main")

    merge = repo.git(main_view, "merge", "-q", "--ff-only", "lane")
    reset = repo.git(main_view, "reset", "-q", "--hard", "lane")
    update_ref = repo.git(lane, "update-ref", "refs/heads/main", "lane")
    delete_ref = repo.git(lane, "update-ref", "-d", "refs/heads/main")
    # git itself refuses to force a branch checked out in another worktree,
    # before any reference transaction starts.
    branch_force = repo.git(lane, "branch", "-f", "main", "lane")

    assert lane_commit.returncode == 0, lane_commit.stderr
    for result in (merge, reset, update_ref):
        _assert_refused(result, "src/gobby/a.py")
    assert delete_ref.returncode != 0
    assert "the protected branch cannot be deleted" in delete_ref.stderr
    assert branch_force.returncode != 0
    assert repo.ok(repo.root, "rev-parse", "main") == main_before


def test_pack_refs_prunes_the_protected_ref_but_deletions_stay_refused(tmp_path: Path) -> None:
    """pack-refs may prune the protected ref's loose copy; deleting the branch stays refused."""
    repo = _baseline(tmp_path)
    lane = tmp_path / "lane"
    repo.ok(repo.root, "worktree", "add", "-q", "-b", "lane", str(lane))
    loose_main = repo.root / ".git" / "refs" / "heads" / "main"

    maintenance = repo.git(lane, "maintenance", "run", "--task=pack-refs")
    pruned_by_maintenance = not loose_main.exists()
    readme = repo.commit(repo.root, {"README.md": "readme\n"})
    main_before = repo.ok(repo.root, "rev-parse", "main")
    pack = repo.git(lane, "pack-refs", "--all")
    pruned_by_pack = not loose_main.exists()
    # A deletion naming the old value sends the same "<old> 0" line as the prune.
    delete_with_old = repo.git(lane, "update-ref", "-d", "refs/heads/main", main_before)
    delete_ref = repo.git(lane, "update-ref", "-d", "refs/heads/main")
    # git refuses to delete a branch checked out in a worktree before any transaction.
    branch_delete = repo.git(lane, "branch", "-D", "main")

    assert maintenance.returncode == 0, maintenance.stderr
    assert readme.returncode == 0, readme.stderr
    assert pack.returncode == 0, pack.stderr
    assert (pruned_by_maintenance, pruned_by_pack) == (True, True)
    for result in (delete_with_old, delete_ref):
        assert result.returncode != 0
        assert "the protected branch cannot be deleted" in result.stderr
    assert branch_delete.returncode != 0
    assert repo.ok(repo.root, "rev-parse", "main") == main_before


def test_guard_classifies_unusual_paths_fail_closed(tmp_path: Path) -> None:
    repo = _baseline(tmp_path)

    mixed = repo.commit(repo.root, {"docs/a.md": "a\n", "src/gobby/a.py": "a = 1\n"})
    repo.unstage(repo.root, "docs/a.md", "src/gobby/a.py")
    quoted = {}
    for path in ("docs/tab\there.md", "docs/new\nline.md"):
        quoted[path] = repo.commit(repo.root, {path: "q\n"})
        repo.unstage(repo.root, path)
    agents = repo.commit(repo.root, {"src/gobby/AGENTS.md": "agents\n"})
    repo.unstage(repo.root, "src/gobby/AGENTS.md")
    spaced = repo.commit(repo.root, {"docs/with space.md": "s\n"})
    accented = repo.commit(repo.root, {"docs/résumé.md": "r\n"})

    _assert_refused(mixed, "src/gobby/a.py")
    for result in quoted.values():
        assert result.returncode != 0
        assert '"docs/' in result.stderr
        assert "is not a direct-commit path" in result.stderr
    _assert_refused(agents, "src/gobby/AGENTS.md")
    assert spaced.returncode == 0, spaced.stderr
    assert accented.returncode == 0, accented.stderr


def test_guard_resolves_reftable_head_and_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "reftable").mkdir()
    (tmp_path / "corrupt").mkdir()
    reftable = _baseline(tmp_path / "reftable", "--ref-format=reftable")
    corrupt = _baseline(tmp_path / "corrupt")
    reftable_lane = tmp_path / "reftable" / "lane"
    corrupt_lane = tmp_path / "corrupt" / "lane"
    reftable.ok(reftable.root, "worktree", "add", "-q", "-b", "lane", str(reftable_lane))
    corrupt.ok(corrupt.root, "worktree", "add", "-q", "-b", "lane", str(corrupt_lane))
    reftable_commit = reftable.commit(reftable_lane, {"src/gobby/a.py": "a = 1\n"})
    main_before = reftable.ok(reftable.root, "rev-parse", "main")
    # The reftable HEAD file is a stub that names no real branch.
    head_stub = (reftable.root / ".git" / "HEAD").read_text(encoding="utf-8")
    update_ref = reftable.git(reftable_lane, "update-ref", "refs/heads/main", "lane")
    (corrupt.root / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    lane_commit = corrupt.commit(corrupt_lane, {"tests/test_a.py": "a = 1\n"})

    assert reftable_commit.returncode == 0, reftable_commit.stderr
    assert head_stub == "ref: refs/heads/.invalid\n"
    _assert_refused(update_ref, "src/gobby/a.py")
    assert reftable.ok(reftable.root, "rev-parse", "main") == main_before
    assert lane_commit.returncode != 0
    assert "cannot resolve the main checkout's HEAD" in lane_commit.stderr
    assert LAND_COMMIT_TEXT in lane_commit.stderr


CHAINED_RECORDER = (
    "#!/bin/sh\n"
    "# foreign hook: record the state argument and stdin\n"
    'printf "== %s %s\\n" "${0##*/}" "$*" >> "$CHAINED_LOG"\n'
    'cat >> "$CHAINED_LOG"\n'
)


def _chain_recorder(repo: _Repo, tmp_path: Path) -> Path:
    """Install the guard the way the installer chains it ahead of a foreign hook."""
    hook = repo.root / ".git" / "hooks" / "reference-transaction"
    hook.write_text(CHAINED_RECORDER, encoding="utf-8")
    assert install_git_hooks(repo.root)["success"] is True
    log = tmp_path / "chained.log"
    log.touch()
    repo.env["CHAINED_LOG"] = str(log)
    return log


def _without_tmp_writes(repo: _Repo, log: Path) -> list[str]:
    """Return a seatbelt prefix leaving git only its own directory and the log.

    Ignoring TMPDIR, bash 3.2 writes here-documents to /tmp or /var/tmp, else
    to the working directory. Seatbelt does not nest, so a sandboxed seat
    skips, as does any platform without sandbox-exec.
    """
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        pytest.skip("needs macOS sandbox-exec")
    root = repo.root.resolve()
    profile = (
        "(version 1)(allow default)"
        '(deny file-write* (subpath "/private/tmp") (subpath "/private/var/tmp")'
        f' (subpath "{root.parent}"))'
        f'(allow file-write* (subpath "{root / ".git"}") (literal "{log.resolve()}"))'
    )
    probe = subprocess.run(
        [sandbox_exec, "-p", profile, "/usr/bin/true"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip(f"seatbelt cannot apply here: {probe.stderr!r}")
    return [sandbox_exec, "-p", profile]


def test_guard_and_chained_hook_need_no_writable_temp_directory(tmp_path: Path) -> None:
    repo = _baseline(tmp_path)
    log = _chain_recorder(repo, tmp_path)
    old = repo.ok(repo.root, "rev-parse", "main")
    repo.wrapper = _without_tmp_writes(repo, log)

    allowed = repo.commit(repo.root, {"docs/ok.md": "ok\n"})
    new = repo.ok(repo.root, "rev-parse", "main")
    refused = repo.commit(repo.root, {"src/gobby/x.py": "x = 2\n"})

    assert allowed.returncode == 0, allowed.stderr
    _assert_refused(refused, "src/gobby/x.py")
    assert repo.ok(repo.root, "rev-parse", "main") == new
    assert f"== reference-transaction prepared\n{old} {new} refs/heads/main\n" in log.read_text()


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda text: text.replace("\n", f"\n{GOBBY_HOOK_END}\n", 1), id="early"),
        pytest.param(lambda text: text.replace(f"{GOBBY_HOOK_END}\n", "", 1), id="removed"),
        pytest.param(lambda text: f"{text}{GOBBY_HOOK_END}\n", id="trailing"),
    ],
)
def test_misplaced_end_marker_cannot_bypass_the_guard(
    tmp_path: Path, tamper: Callable[[str], str]
) -> None:
    """The replay runs this file's own text after the guard passed, at most once."""
    repo = _baseline(tmp_path)
    log = _chain_recorder(repo, tmp_path)
    hook = repo.root / ".git" / "hooks" / "reference-transaction"
    hook.write_text(tamper(hook.read_text(encoding="utf-8")), encoding="utf-8")

    refused = repo.commit(repo.root, {"src/gobby/x.py": "x = 2\n"})
    repo.unstage(repo.root, "src/gobby/x.py")
    main_before = repo.ok(repo.root, "rev-parse", "main")
    allowed = repo.commit(repo.root, {"docs/ok.md": "ok\n"})

    _assert_refused(refused, "src/gobby/x.py")
    assert allowed.returncode == 0, allowed.stderr
    assert repo.ok(repo.root, "rev-parse", "main~1") == main_before
    assert "== reference-transaction prepared\n" in log.read_text()


async def test_land_commit_passes_installed_guard(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    repo = _baseline(tmp_path)
    # Repository-local hooksPath, so land_commit's git finds the guard under any global config.
    repo.ok(repo.root, "config", "core.hooksPath", str(repo.root / ".git" / "hooks"))
    lane = tmp_path / "lane"
    repo.ok(repo.root, "worktree", "add", "-q", "-b", "lane", str(lane))
    candidate = repo.commit(lane, {"tests/test_a.py": "a = 1\n"})
    sha = repo.ok(lane, "rev-parse", "HEAD")
    direct = repo.git(repo.root, "merge", "-q", "--ff-only", "lane")
    creator, claimant, reviewer = (
        SessionManager(temp_db).register(
            external_id=external_id,
            machine_id=isolated.machine_id,
            source="codex",
            project_id=isolated.project.id,
        )
        for external_id in ("creator", "claimant", "reviewer")
    )
    task = LocalTaskManager(temp_db).create_task(
        isolated.project.id,
        "Reviewed candidate",
        created_in_session_id=creator.id,
        claimed_by_session_id=claimant.id,
        validation_criteria="Land the reviewed SHA.",
    )
    temp_db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), task.id))
    task = LocalTaskManager(temp_db).get_task(task.id)
    record_close_receipt(
        temp_db,
        task=task,
        author_session_id=reviewer.id,
        kind=INDEPENDENT_REVIEW_APPROVAL,
        commit_sha=sha,
    )

    with session_context_for_test(reviewer.id):
        landed = await land_candidate(
            temp_db, task=task, caller_session_id=reviewer.id, commit_sha=sha
        )

    assert candidate.returncode == 0, candidate.stderr
    _assert_refused(direct, "tests/test_a.py")
    assert landed["landed"] is True, landed
    assert repo.ok(repo.root, "rev-parse", "main") == sha
