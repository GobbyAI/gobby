"""Landing boundaries exercised with isolated PostgreSQL and throwaway Git repositories."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks import land_commit
from gobby.tasks.close_receipts import (
    INDEPENDENT_REVIEW_APPROVAL,
    LANDING,
    LANDING_APPROVAL,
    list_close_receipts,
    record_close_receipt,
)
from gobby.tasks.commits import extract_task_ids_from_message
from gobby.tasks.land_commit import LandingResult, land_candidate
from gobby.tasks.landing_policy import write_freeze
from gobby.utils.daemon_git import GitOk, GitResult, GitTimeout, daemon_git
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@dataclass
class LandingCase:
    db: HubDatabase
    repo: Path
    project_id: str
    creator: str
    claimant: str
    reviewer: str
    delegator: str
    base: str

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                **(env or {}),
            },
        )
        return result.stdout.strip()

    def candidate(self, branch: str, files: dict[str, str], base: str | None = None) -> str:
        self.git("switch", "-c", branch, base or self.base)
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "--all")
        self.git("commit", "-m", "test candidate")
        sha = self.git("rev-parse", "HEAD")
        self.git("switch", "trunk")
        return sha

    def task(self, title: str = "Reviewed candidate") -> Task:
        task = LocalTaskManager(self.db).create_task(
            self.project_id,
            title,
            created_in_session_id=self.creator,
            claimed_by_session_id=self.claimant,
            validation_criteria="Land the reviewed SHA.",
        )
        self.db.execute(
            "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s", (self.delegator, task.id)
        )
        return LocalTaskManager(self.db).get_task(task.id)

    def link(self, task: Task, sha: str) -> None:
        self.db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), task.id))

    def approve(self, task: Task, sha: str, author: str | None = None) -> None:
        record_close_receipt(
            self.db,
            task=task,
            author_session_id=author or self.reviewer,
            kind=INDEPENDENT_REVIEW_APPROVAL,
            commit_sha=sha,
        )

    def reviewed(self, sha: str, title: str = "Reviewed candidate") -> Task:
        task = self.task(title)
        self.link(task, sha)
        self.approve(task, sha)
        return task

    def direct(self, files: dict[str, str]) -> str:
        """Commit only ``files`` onto trunk, as a direct commit beside staged work does."""
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "--", *files)
        self.git("commit", "-m", "direct commit", "--", *files)
        return self.git("rev-parse", "HEAD")

    async def land(self, task: Task, sha: str, caller: str | None = None) -> LandingResult:
        session = caller or self.reviewer
        with session_context_for_test(session):
            return await land_candidate(
                self.db, task=task, caller_session_id=session, commit_sha=sha
            )


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    repo = tmp_path / "main"
    repo.mkdir()
    identities: list[str] = []
    for index, role in enumerate(("creator", "claimant", "reviewer", "delegator")):
        session_id = str(uuid4())
        temp_db.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id, title,
                                  status, agent_depth, seq_num)
            VALUES (%s, %s, %s, 'test', %s, %s, 'active', 0, %s)
            """,
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                sample_project["id"],
                role,
                600 + index,
            ),
        )
        identities.append(session_id)
    creator, claimant, reviewer, delegator = identities
    fixture = LandingCase(
        temp_db, repo, sample_project["id"], creator, claimant, reviewer, delegator, ""
    )
    fixture.git("init", "--initial-branch=trunk")
    fixture.git("config", "user.name", "Landing test")
    fixture.git("config", "user.email", "landing@example.invalid")
    fixture.git("config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    (repo / "README.md").write_text("base\n")
    fixture.git("add", "README.md")
    fixture.git("commit", "-m", "initial")
    fixture.base = fixture.git("rev-parse", "HEAD")
    LocalProjectCheckoutManager(temp_db).rebind(require_machine_id(), fixture.project_id, str(repo))
    return fixture


Proceed = Callable[[], Awaitable[GitResult]]


@contextmanager
def _intercept_fast_forward(
    hook: Callable[[Proceed, Sequence[str], float], Awaitable[GitResult]],
) -> Iterator[None]:
    """Route each ``merge --ff-only`` through ``hook``, which may run the real command."""
    original_run = daemon_git.run

    async def run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        def proceed() -> Awaitable[GitResult]:
            return original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

        if tuple(args[:2]) == ("merge", "--ff-only"):
            return await hook(proceed, args, timeout)
        return await proceed()

    with patch.object(daemon_git, "run", side_effect=run):
        yield


async def test_refuses_without_callers_land_receipt(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "review me"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha, author=case.creator)
    missing = await case.land(task, sha)
    assert missing["error"] == "review_receipt_missing"
    assert case.git("rev-parse", "trunk") == case.base
    case.approve(task, sha)
    case.db.execute(
        "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s", (case.reviewer, task.id)
    )
    claimant = await case.land(task, sha)
    assert claimant["error"] == "caller_is_claimant"
    assert case.git("rev-parse", "trunk") == case.base
    assert all(receipt.kind != LANDING for receipt in list_close_receipts(case.db, task.id))


@pytest.mark.parametrize("release", ["escalation", "transfer"])
async def test_former_claimant_cannot_land_its_own_task(case: LandingCase, release: str) -> None:
    sha = case.candidate("lane", {"docs/change.md": "review me"})
    task = case.task()
    case.link(task, sha)
    manager = LocalTaskManager(case.db)
    if release == "escalation":
        manager.escalate_task(task.id, "Handed off: review pending")
    else:
        manager.claim_task(task.id, case.delegator, force=True)
    case.approve(task, sha)
    refused = await case.land(task, sha, caller=case.claimant)
    assert refused["error"] == "caller_was_claimant"
    assert case.git("rev-parse", "trunk") == case.base
    landed = await case.land(task, sha)
    assert landed["landed"] is True
    assert case.git("rev-parse", "trunk") == sha


async def test_fast_forward_lands_exact_candidate(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "land me"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    result = await case.land(task, sha)
    assert result["landed"] is True
    assert result["mode"] == "ff"
    assert result["branch"] == "trunk"
    assert result["landed_tip"] == sha
    assert result["observed_tip"] == sha
    assert result["activation_class"] == "none"
    assert result["retest_required"] is False
    assert case.git("rev-parse", "trunk") == sha
    assert (case.repo / "docs/change.md").read_text() == "land me"
    [receipt] = [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    assert receipt.author_session_id == case.reviewer
    assert receipt.commit_sha == sha
    assert receipt.facts["provenance"] == "recorded"
    manager = InterSessionMessageManager(case.db)
    for recipient in (case.claimant, case.creator, case.delegator):
        [message] = manager.get_messages(recipient)
        assert message.from_session == case.reviewer
        assert f'Landed #{task.seq_num} "{task.title}" {sha[:10]} on trunk' in message.content
    assert manager.get_messages(case.reviewer) == []


async def test_reports_every_missing_approval_at_once(case: LandingCase) -> None:
    files = {"src/gobby/cli/entry.py": "candidate", "docs/line\nbreak.md": "candidate"}
    sha = case.candidate("lane", files)
    other = case.candidate("other", dict.fromkeys(files, "other"))
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    other_task = case.task("Other in-flight candidate")
    case.approve(other_task, other)
    write_freeze(case.repo / ".git", on=True, reason="release hold", session_id=case.creator)
    refused = await case.land(task, sha)
    assert refused["landed"] is False
    assert refused["missing_approvals"] == ["freeze", "overlap", "restart"]
    assert refused["activation_class"] == "restart"
    assert refused["commit_sha"] == sha
    assert refused["overlaps"] == [
        {"task_ref": f"#{other_task.seq_num}", "commit_sha": other, "shared_paths": sorted(files)}
    ]
    assert case.git("rev-parse", "trunk") == case.base
    for author, reason in ((case.creator, "restart"), (case.delegator, "freeze,overlap")):
        record_close_receipt(
            case.db,
            task=task,
            author_session_id=author,
            kind=LANDING_APPROVAL,
            commit_sha=sha,
            facts={"reason": reason},
        )
    landed = await case.land(task, sha)
    assert landed["landed"] is True
    assert case.git("rev-parse", "trunk") == sha


async def test_stacked_and_landed_candidates_never_overlap(case: LandingCase) -> None:
    ancestor = case.candidate("ancestor", {"docs/shared.md": "ancestor"})
    sha = case.candidate("lane", {"docs/shared.md": "candidate"}, base=ancestor)
    descendant = case.candidate("descendant", {"docs/shared.md": "descendant"}, base=sha)
    closed = case.candidate("closed", {"docs/shared.md": "closed sibling"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    for name, candidate in (
        ("Ancestor", ancestor),
        ("Descendant", descendant),
        ("Already landed", case.base),
    ):
        case.approve(case.task(name), candidate)
    closed_task = case.task("Closed candidate")
    case.approve(closed_task, closed)
    case.db.execute("UPDATE tasks SET closed_at = now() WHERE id = %s", (closed_task.id,))
    result = await case.land(task, sha)
    assert result["landed"] is True
    assert result["activation_class"] == "none"
    assert case.git("rev-parse", "trunk") == sha


async def test_unavailable_foreign_candidate_does_not_block_landing(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "candidate"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    foreign_task = case.task("Reviewed in another object store")
    unavailable = "1" * 40
    case.link(foreign_task, unavailable)
    case.approve(foreign_task, unavailable)
    result = await case.land(task, sha)
    assert result["landed"] is True
    assert result["mode"] == "ff"
    assert case.git("rev-parse", "trunk") == sha
    [foreign_receipt] = list_close_receipts(case.db, foreign_task.id)
    assert foreign_receipt.commit_sha == unavailable


async def test_refuses_unlinked_candidate(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "linked later"})
    task = case.task()
    case.approve(task, sha)
    case.link(task, case.base)
    refused = await case.land(task, sha)
    assert refused["error"] == "candidate_not_linked"
    assert case.git("rev-parse", "trunk") == case.base
    # Stored short links are resolved through Git, not compared as SHA prefixes.
    case.link(task, sha[:12])
    landed = await case.land(task, sha)
    assert landed["landed"] is True
    assert landed["landed_tip"] == sha


async def test_landing_branch_is_main_checkout_head(case: LandingCase, tmp_path: Path) -> None:
    sha = case.candidate("lane", {"docs/change.md": "candidate"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    case.git("switch", "--detach", case.base)
    detached = await case.land(task, sha)
    assert detached["error"] == "main_checkout_detached"
    assert case.git("rev-parse", "trunk") == case.base
    case.git("switch", "trunk")
    original_branch = land_commit._branch
    reads = 0

    async def changed_branch(main: Path) -> str | None:
        nonlocal reads
        reads += 1
        if reads == 2:
            case.git("switch", "-c", "different", case.base)
        return await original_branch(main)

    with patch("gobby.tasks.land_commit._branch", side_effect=changed_branch):
        changed = await case.land(task, sha)
    assert changed["error"] == "main_checkout_branch_changed"
    assert case.git("rev-parse", "trunk") == case.base
    assert case.git("rev-parse", "different") == case.base
    case.git("switch", "trunk")
    caller_path = tmp_path / "linked-caller"
    case.git("worktree", "add", str(caller_path), "lane")
    LocalProjectCheckoutManager(case.db).rebind(
        require_machine_id(), case.project_id, str(caller_path)
    )
    try:
        caller_branch = await daemon_git.run(("symbolic-ref", "--short", "HEAD"), cwd=caller_path)
        assert isinstance(caller_branch, GitOk)
        assert caller_branch.stdout.strip() == "lane"
        landed = await case.land(task, sha)
        assert landed["landed"] is True
        assert landed["branch"] == "trunk"
        assert case.git("rev-parse", "trunk") == sha
    finally:
        LocalProjectCheckoutManager(case.db).rebind(
            require_machine_id(), case.project_id, str(case.repo)
        )
        case.git("worktree", "remove", str(caller_path))


async def test_concurrent_landings_serialize_and_release_lock(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "candidate"})
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    original_run = daemon_git.run
    writing = asyncio.Event()
    release = asyncio.Event()
    second_branch_read = asyncio.Event()
    writes = 0

    async def gated_run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        nonlocal writes
        current = asyncio.current_task()
        if args[0] == "symbolic-ref" and current is not None and current.get_name() == "landing-2":
            second_branch_read.set()
        if tuple(args[:2]) == ("merge", "--ff-only"):
            writes += 1
            writing.set()
            await release.wait()
        return await original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

    async def freeze_during_landing() -> dict[str, Any]:
        with session_context_for_test(case.creator):
            return dict(
                await create_task_ops_registry(LocalTaskManager(case.db)).call(
                    "set_landing_freeze", {"on": True, "reason": "release hold"}
                )
            )

    with patch.object(daemon_git, "run", side_effect=gated_run):
        first = asyncio.create_task(case.land(task, sha), name="landing-1")
        try:
            await asyncio.wait_for(writing.wait(), timeout=5)
            freezing = asyncio.create_task(freeze_during_landing())
            second = asyncio.create_task(case.land(task, sha), name="landing-2")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_branch_read.wait(), timeout=0.1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(freezing), timeout=0.2)
        finally:
            release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert (await freezing)["success"] is True
    assert first_result == second_result
    assert first_result["landed"] is True
    assert writes == 1
    with patch("gobby.tasks.land_commit._paths", side_effect=RuntimeError("probe")):
        later = case.candidate("later", {"docs/next.md": "next"}, base=sha)
        later_task = case.task("Next candidate")
        case.link(later_task, later)
        case.approve(later_task, later)
        with pytest.raises(RuntimeError, match="probe"):
            await case.land(later_task, later)
    async with case.db.advisory_lock(MainCheckoutLanding(case.project_id)):
        assert case.git("rev-parse", "trunk") == sha
    refused = await case.land(later_task, later)
    assert refused["missing_approvals"] == ["freeze"]
    async with case.db.advisory_lock(MainCheckoutLanding(case.project_id)):
        assert case.git("rev-parse", "trunk") == sha
    with session_context_for_test(case.creator):
        cleared = await create_task_ops_registry(LocalTaskManager(case.db)).call(
            "set_landing_freeze", {"on": False, "reason": "release"}
        )
    assert cleared["success"] is True
    assert (await case.land(later_task, later))["landed"] is True


@pytest.mark.parametrize(
    "scenario",
    [
        "ff",
        "two_parent_ff",
        "merge",
        "stacked",
        "expired",
        "unreadable",
        "non_files",
        "malformed_line",
        "malformed_action",
        "foreign_writer",
        "zero_old",
        "missing_old",
        "missing_new",
        "missing_action_candidate",
        "closed",
        "rewound",
    ],
)
async def test_already_landed_candidate_records_landing_once(
    case: LandingCase, scenario: str
) -> None:
    sha = case.candidate("lane", {"docs/change.md": "candidate"})
    target = sha
    mode = "ff"
    if scenario in {"two_parent_ff", "merge"}:
        side = case.candidate("side", {"docs/side.md": "side"})
        case.git("switch", "lane")
        case.git("merge", "--no-ff", side, "-m", "reviewed two-parent candidate")
        target = case.git("rev-parse", "HEAD")
        case.git("switch", "trunk")
        if scenario == "two_parent_ff":
            sha = target
        else:
            mode = "merge"
    elif scenario == "stacked":
        target = case.candidate("stack", {"src/gobby/change.py": "restart"}, base=sha)
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    activation = "restart" if scenario == "stacked" else "none"
    action_candidate = sha if scenario == "merge" else target
    action = f"gobby-land candidate={action_candidate} mode={mode} class={activation}"
    if scenario == "missing_action_candidate":
        action = f"gobby-land candidate={'f' * 40} mode=ff class=none"
    if scenario == "foreign_writer":
        action = "foreign update"
    if scenario == "closed":
        case.db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    case.git("merge", "--ff-only", target, env={"GIT_REFLOG_ACTION": action})
    log = case.repo / ".git/logs/refs/heads/trunk"
    if scenario == "expired":
        log.unlink()
    elif scenario == "unreadable":
        log.unlink()
        log.mkdir()
    elif scenario == "malformed_line":
        log.write_text(log.read_text() + "broken line\n")
    elif scenario == "malformed_action":
        log.write_text(log.read_text().replace(action, "gobby-land bad action"))
    elif scenario in {"zero_old", "missing_old", "missing_new"}:
        lines = log.read_text().splitlines()
        fields = lines[-1].split(" ", 2)
        if scenario == "missing_new":
            fields[1] = "f" * 40
        else:
            fields[0] = ("0" if scenario == "zero_old" else "f") * 40
        lines[-1] = " ".join(fields)
        log.write_text("\n".join(lines) + "\n")
    elif scenario == "rewound":
        descendant = case.candidate("past-event", {"docs/past.md": "past"}, base=sha)
        case.git(
            "merge",
            "--ff-only",
            descendant,
            env={"GIT_REFLOG_ACTION": f"gobby-land candidate={sha} mode=ff class=none"},
        )
        case.git("reset", "--hard", sha)
        log.write_text(log.read_text().replace(action, "foreign update", 1))

    original_run = daemon_git.run

    async def non_files_run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        result = await original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)
        if tuple(args) == ("rev-parse", "--show-ref-format"):
            return GitOk(status="ok", argv=tuple(args), stdout="reftable\n", stderr="")
        return result

    with patch.object(
        daemon_git, "run", side_effect=non_files_run if scenario == "non_files" else original_run
    ):
        first = await case.land(task, sha)
    if scenario == "unreadable":
        log.rmdir()
    write_freeze(case.repo / ".git", on=True, reason="later freeze", session_id=case.creator)
    case.git("commit", "--allow-empty", "-m", "later docs writer")
    manager = InterSessionMessageManager(case.db)
    original_messages = manager.get_messages(case.claimant)
    second = await case.land(task, sha)
    assert first == second
    assert first["landed"] is True
    recovered = scenario in {"ff", "two_parent_ff", "merge", "stacked", "closed"}
    assert first["provenance"] == ("reflog" if recovered else "unknown")
    assert first["mode"] == (mode if recovered else "already_landed")
    assert first["activation_class"] == ("none" if recovered else "unknown")
    assert first["retest_required"] is (not recovered or mode == "merge")
    if mode == "merge":
        assert first["merge_commit"] == target
    receipts = [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    assert len(receipts) == 1
    assert manager.get_messages(case.claimant) == original_messages
    assert case.git("rev-parse", "trunk") != first["observed_tip"]


async def test_foreign_fast_forward_uses_stored_old_tip(case: LandingCase) -> None:
    a = case.candidate("ancestor", {"docs/a.md": "A"})
    b = case.candidate("descendant", {"docs/b.md": "B"}, base=a)
    task_b = case.task("B")
    case.link(task_b, b)
    case.approve(task_b, b)
    original_run = daemon_git.run

    async def race_run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        if tuple(args[:2]) == ("merge", "--ff-only"):
            case.git("merge", "--ff-only", a)
        return await original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

    with patch.object(daemon_git, "run", side_effect=race_run):
        assert (await case.land(task_b, b))["landed"] is True
    replay_b = case.task("B replay without prior receipt")
    case.link(replay_b, b)
    case.approve(replay_b, b)
    result_b = await case.land(replay_b, b)
    task_a = case.task("A foreign landing")
    case.link(task_a, a)
    case.approve(task_a, a)
    result_a = await case.land(task_a, a)
    assert (result_b["mode"], result_b["provenance"]) == ("ff", "reflog")
    assert (result_a["mode"], result_a["provenance"], result_a["activation_class"]) == (
        "already_landed",
        "unknown",
        "unknown",
    )
    assert case.git("rev-parse", "trunk") == b


async def test_landing_records_target_when_observed_tip_advances(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/change.md": "candidate"})
    ahead = case.candidate("ahead", {"docs/next.md": "next"}, base=sha)
    task = case.task()
    case.link(task, sha)
    case.approve(task, sha)
    original_run = daemon_git.run

    async def advancing_run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        result = await original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)
        if tuple(args[:2]) == ("merge", "--ff-only"):
            assert env is not None and env["GOBBY_LAND_COMMIT"] == "1"
            assert env["GIT_REFLOG_ACTION"] == f"gobby-land candidate={sha} mode=ff class=none"
            case.git("merge", "--ff-only", ahead)
        return result

    with patch.object(daemon_git, "run", side_effect=advancing_run):
        result = await case.land(task, sha)
    assert result["landed"] is True
    assert (result["landed_tip"], result["observed_tip"]) == (sha, ahead)
    assert case.git("rev-parse", "trunk") == ahead
    [receipt] = [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    assert (receipt.facts["landed_tip"], receipt.facts["observed_tip"]) == (sha, ahead)


async def test_moved_tip_with_disjoint_paths_lands_merge_commit(case: LandingCase) -> None:
    sha = case.candidate("lane", {"tests/test_lane.py": "lane = 1\n"})
    moved = case.direct({"docs/moved.md": "moved\n"})
    task = case.reviewed(sha)
    (case.repo / "docs" / "staged.md").write_text("staged\n")
    case.git("add", "docs/staged.md")
    (case.repo / "README.md").write_text("dirty\n")
    (case.repo / "notes.txt").write_text("untracked\n")

    result = await case.land(task, sha)

    merge = case.git("rev-parse", "trunk")
    assert result["landed"] is True, result
    assert (result["mode"], result["retest_required"]) == ("merge", True)
    assert result["landed_tip"] == result["merge_commit"] == merge
    assert "focused verification commands" in result["retest_procedure"]
    assert case.git("rev-list", "--parents", "-n", "1", merge).split() == [merge, moved, sha]
    assert case.git("log", "-1", "--format=%s", merge) == (
        f"chore: land reviewed {sha[:10]} for #{task.seq_num}"
    )
    [receipt] = [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    assert (receipt.facts["mode"], receipt.facts["merge_commit"]) == ("merge", merge)
    assert receipt.facts["retest_required"] is True
    assert case.git("diff", "--cached", "--name-only") == "docs/staged.md"
    assert (case.repo / "README.md").read_text() == "dirty\n"
    assert (case.repo / "notes.txt").read_text() == "untracked\n"
    assert (case.repo / "tests" / "test_lane.py").read_text() == "lane = 1\n"


async def test_moved_tip_with_shared_paths_requires_base_update(case: LandingCase) -> None:
    sha = case.candidate("lane", {"docs/shared.md": "lane\n", "src/gobby/lane.py": "x = 1\n"})
    moved = case.direct({"docs/shared.md": "moved\n"})
    task = case.reviewed(sha)

    result = await case.land(task, sha)

    assert result["landed"] is False
    assert result["blockers"] == ["landing_approval_missing", "base_update_required"]
    assert (result["shared_paths"], result["missing_approvals"]) == (
        ["docs/shared.md"],
        ["restart"],
    )
    assert case.git("rev-parse", "trunk") == moved


def _criss_cross_candidate(case: LandingCase) -> str:
    left = case.candidate("left", {"src/gobby/left.py": "left = 1\n"})
    right = case.candidate("right", {"src/gobby/right.py": "right = 1\n"})
    case.git("merge", "--ff-only", left)
    case.git("merge", "--no-ff", right, "-m", "tip merges right")
    case.git("switch", "-c", "lane", right)
    case.git("merge", "--no-ff", left, "-m", "lane merges left")
    lane_base = case.git("rev-parse", "HEAD")
    case.git("switch", "trunk")
    sha = case.candidate("docs-lane", {"docs/lane.md": "lane\n"}, base=lane_base)
    tip = case.git("rev-parse", "trunk")
    assert len(case.git("merge-base", "--all", tip, sha).splitlines()) == 2
    arbitrary_base = case.git("merge-base", tip, sha)
    assert "src/gobby/" in case.git("diff", "--name-only", arbitrary_base, sha)
    return sha


async def test_clean_merge_of_moved_shared_path_still_requires_base_update(
    case: LandingCase,
) -> None:
    lines = [f"line {index}\n" for index in range(20)]
    base = case.direct({"docs/shared.md": "".join(lines)})
    sha = case.candidate("lane", {"docs/shared.md": "lane\n" + "".join(lines[1:])}, base=base)
    tip = case.direct({"docs/shared.md": "".join(lines[:-1]) + "moved\n"})
    assert case.git("merge-tree", "--write-tree", tip, sha)

    result = await case.land(case.reviewed(sha), sha)

    assert result["landed"] is False
    assert result["blockers"] == ["base_update_required"]
    assert result["shared_paths"] == ["docs/shared.md"]
    assert case.git("rev-parse", "trunk") == tip


async def test_criss_cross_merge_paths_equal_to_tip_are_not_shared(case: LandingCase) -> None:
    sha = _criss_cross_candidate(case)
    other = case.candidate(
        "other",
        {"src/gobby/left.py": "left = 2\n", "src/gobby/right.py": "right = 2\n"},
        base=case.git("rev-parse", "trunk"),
    )
    case.reviewed(other, "Pending source changes")

    result = await case.land(case.reviewed(sha), sha)

    assert result["landed"] is True, result
    assert result["mode"] == "merge"
    assert result["activation_class"] == "none"
    assert case.git("diff", "--name-only", "trunk^1", "trunk") == "docs/lane.md"
    assert (case.repo / "src/gobby/left.py").read_text() == "left = 1\n"
    assert (case.repo / "src/gobby/right.py").read_text() == "right = 1\n"


async def test_criss_cross_genuine_overlap_still_names_changed_path(case: LandingCase) -> None:
    docs = _criss_cross_candidate(case)
    sha = case.candidate("changed-lane", {"src/gobby/left.py": "left = 2\n"}, base=docs)
    other = case.candidate(
        "other", {"src/gobby/left.py": "left = 3\n"}, base=case.git("rev-parse", "trunk")
    )
    other_task = case.reviewed(other, "Pending source change")

    result = await case.land(case.reviewed(sha), sha)

    assert result["landed"] is False
    assert result["overlaps"] == [
        {
            "task_ref": f"#{other_task.seq_num}",
            "commit_sha": other,
            "shared_paths": ["src/gobby/left.py"],
        }
    ]
    assert result["missing_approvals"] == ["overlap", "restart"]


async def test_criss_cross_pending_candidate_does_not_own_carried_source(case: LandingCase) -> None:
    docs = _criss_cross_candidate(case)
    case.reviewed(docs, "Pending docs")
    sha = case.candidate(
        "source-lane",
        {"src/gobby/left.py": "left = 2\n", "src/gobby/right.py": "right = 2\n"},
        base=case.git("rev-parse", "trunk"),
    )
    task = case.reviewed(sha)
    record_close_receipt(
        case.db,
        task=task,
        author_session_id=case.creator,
        kind=LANDING_APPROVAL,
        commit_sha=sha,
        facts={"reason": "restart"},
    )

    result = await case.land(task, sha)

    assert result["landed"] is True, result
    assert result["activation_class"] == "restart"
    assert not (case.repo / "docs/lane.md").exists()
    assert (case.repo / "src/gobby/left.py").read_text() == "left = 2\n"
    assert (case.repo / "src/gobby/right.py").read_text() == "right = 2\n"


async def test_criss_cross_recovery_classifies_only_candidate_contribution(
    case: LandingCase,
) -> None:
    sha = _criss_cross_candidate(case)
    stacked = case.candidate("stacked", {"src/gobby/stacked.py": "stacked = 1\n"}, base=sha)
    tip = case.git("rev-parse", "trunk")
    tree = case.git("merge-tree", "--write-tree", tip, stacked).splitlines()[0]
    merged = case.git("commit-tree", tree, "-p", tip, "-p", stacked, "-m", "land stack")
    case.git(
        "merge",
        "--ff-only",
        merged,
        env={"GIT_REFLOG_ACTION": f"gobby-land candidate={stacked} mode=merge class=restart"},
    )

    result = await case.land(case.reviewed(sha), sha)

    assert result["landed"] is True, result
    assert result["provenance"] == "reflog"
    assert result["activation_class"] == "none"
    assert result["retest_required"] is True
    assert result["merge_commit"] == merged


async def test_tip_race_recomputes_and_lands(case: LandingCase) -> None:
    races: list[Callable[[], object]] = []
    targets: list[str] = []

    async def race(proceed: Proceed, args: Sequence[str], timeout: float) -> GitResult:
        targets.append(args[2])
        if races:
            races.pop(0)()
        return await proceed()

    absorbed = case.candidate("absorbed", {"tests/test_absorbed.py": "a = 1\n"})
    races[:] = [partial(case.direct, {"docs/race.md": "race\n"})]
    with _intercept_fast_forward(race):
        absorbed_result = await case.land(case.reviewed(absorbed, "Absorbed"), absorbed)
    race_tip = case.git("rev-parse", "trunk~1")
    merge = case.git("rev-parse", "trunk")

    shared = case.candidate("shared", {"docs/shared.md": "lane\n"}, base=merge)
    races[:] = [partial(case.direct, {"docs/shared.md": "race\n"})]
    with _intercept_fast_forward(race):
        shared_result = await case.land(case.reviewed(shared, "Shared"), shared)
    shared_tip = case.git("rev-parse", "trunk")

    # An operator-override rewind past a restart-class ancestor moves the merge base.
    restart_ancestor = case.direct({"src/gobby/feature.py": "x = 1\n"})
    restart = case.candidate("restart", {"docs/restart.md": "lane\n"}, base=restart_ancestor)
    rewound = case.candidate("rewound", {"docs/rewound.md": "rewound\n"}, base=shared_tip)
    races[:] = [partial(case.git, "reset", "--hard", rewound)]
    with _intercept_fast_forward(race):
        restart_result = await case.land(case.reviewed(restart, "Restart"), restart)

    contended = case.candidate("contended", {"tests/test_contended.py": "c = 1\n"}, base=rewound)
    races[:] = [partial(case.direct, {f"docs/race-{n}.md": f"{n}\n"}) for n in range(3)]
    targets.clear()
    with _intercept_fast_forward(race):
        contended_result = await case.land(case.reviewed(contended, "Contended"), contended)

    assert absorbed_result["landed"] is True, absorbed_result
    assert absorbed_result["mode"] == "merge"
    assert case.git("rev-list", "--parents", "-n", "1", merge).split() == [
        merge,
        race_tip,
        absorbed,
    ]
    assert (shared_result["error"], shared_result["shared_paths"]) == (
        "base_update_required",
        ["docs/shared.md"],
    )
    assert restart_result["blockers"] == ["landing_approval_missing"]
    assert (restart_result["activation_class"], restart_result["missing_approvals"]) == (
        "restart",
        ["restart"],
    )
    assert contended_result["error"] == "tip_contention"
    assert targets[0] == contended and len(targets) == 3
    assert case.git("log", "--format=%s", f"{rewound}..trunk").splitlines() == ["direct commit"] * 3


async def test_dirty_path_and_index_lock_refuse_without_ref_change(case: LandingCase) -> None:
    moved = case.direct({"docs/clash": "file\n"})
    dirty = case.candidate("dirty", {"README.md": "lane\n"})
    busy = case.candidate("busy", {"docs/busy.md": "busy\n"})
    # Disjoint path sets that still conflict: a file where the candidate needs a directory.
    clash = case.candidate("clash", {"docs/clash/inner.md": "inner\n"})
    contended = case.candidate("contended", {"docs/contended.md": "c\n"}, base=moved)
    racers = [moved]
    for n in range(3):
        racers.append(case.candidate(f"racer-{n}", {f"docs/racer-{n}.md": "r\n"}, base=racers[-1]))
    (case.repo / "docs" / "staged.md").write_text("staged\n")
    case.git("add", "docs/staged.md")
    (case.repo / "notes.txt").write_text("untracked\n")
    (case.repo / "README.md").write_text("dirty\n")
    lock = case.repo / ".git" / "index.lock"

    def checkout() -> tuple[str, str, str, dict[str, str]]:
        return (
            case.git("rev-parse", "trunk"),
            case.git("ls-files", "--stage"),
            case.git("ls-files", "--others", "--exclude-standard"),
            {
                name: (case.repo / name).read_text()
                for name in ("README.md", "docs/staged.md", "notes.txt")
            },
        )

    async def race(proceed: Proceed, args: Sequence[str], timeout: float) -> GitResult:
        case.git("update-ref", "refs/heads/trunk", racers.pop(1))
        return await proceed()

    before = checkout()
    dirty_result = await case.land(case.reviewed(dirty, "Dirty"), dirty)
    after_dirty = checkout()
    lock.touch()
    busy_result = await case.land(case.reviewed(busy, "Busy"), busy)
    lock.unlink()
    after_busy = checkout()
    clash_result = await case.land(case.reviewed(clash, "Clash"), clash)
    after_clash = checkout()
    with _intercept_fast_forward(race):
        contended_result = await case.land(case.reviewed(contended, "Contended"), contended)
    after_contention = checkout()

    assert [
        result["error"] for result in (dirty_result, busy_result, clash_result, contended_result)
    ] == ["checkout_dirty", "checkout_busy", "merge_conflict", "tip_contention"]
    assert "would be overwritten" in dirty_result["message"]
    assert "README.md" in dirty_result["message"]
    assert "index.lock" in busy_result["message"]
    assert "CONFLICT" in clash_result["message"]
    assert before[0] == moved
    assert after_dirty == after_busy == after_clash == before
    assert after_contention[0] == case.git("rev-parse", "racer-2")
    assert after_contention[1:] == before[1:]


async def test_fast_forward_runs_git_in_c_locale(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A translated "would be overwritten" would read as git_failed instead of checkout_dirty.
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    sha = case.candidate("dirty", {"README.md": "lane\n"})
    (case.repo / "README.md").write_text("dirty\n")
    locales: list[str | None] = []
    original_run = daemon_git.run

    async def recording_run(
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        if tuple(args[:2]) == ("merge", "--ff-only"):
            locales.append(None if env is None else env.get("LC_ALL"))
        return await original_run(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

    with patch.object(daemon_git, "run", side_effect=recording_run):
        result = await case.land(case.reviewed(sha, "Dirty"), sha)

    assert locales == ["C"]
    assert result["error"] == "checkout_dirty"


async def test_landing_merge_is_not_task_tagged(case: LandingCase) -> None:
    sha = case.candidate("lane", {"tests/test_lane.py": "lane = 1\n"})
    case.direct({"docs/moved.md": "moved\n"})
    task = case.reviewed(sha)

    result = await case.land(task, sha)

    message = case.git("log", "-1", "--format=%B", result["merge_commit"])
    assert message == f"chore: land reviewed {sha[:10]} for #{task.seq_num}"
    assert extract_task_ids_from_message(message) == []


async def test_ref_moved_before_failure_reports_landing(case: LandingCase) -> None:
    chain = [case.base]
    for name in ("timed-out", "receipt", "message", "advanced", "ahead", "late", "later", "idle"):
        chain.append(case.candidate(name, {f"docs/{name}.md": f"{name}\n"}, base=chain[-1]))
    timed_out, receipt, message, advanced, ahead, late, later, idle = chain[1:]
    (case.repo / "README.md").write_text("dirty\n")
    (case.repo / "notes.txt").write_text("untracked\n")
    timeouts: list[float] = []

    def timeout_result(args: Sequence[str], timeout: float) -> GitTimeout:
        return GitTimeout("timeout", ("git", *args), timeout)

    async def moved_then_timeout(
        proceed: Proceed, args: Sequence[str], timeout: float
    ) -> GitResult:
        timeouts.append(timeout)
        await proceed()
        return timeout_result(args, timeout)

    async def advance_after(proceed: Proceed, args: Sequence[str], timeout: float) -> GitResult:
        result = await proceed()
        case.git("merge", "--ff-only", ahead)
        return result

    async def advance_then_timeout(
        proceed: Proceed, args: Sequence[str], timeout: float
    ) -> GitResult:
        await proceed()
        case.git("merge", "--ff-only", later)
        return timeout_result(args, timeout)

    async def interrupted(proceed: Proceed, args: Sequence[str], timeout: float) -> GitResult:
        return timeout_result(args, timeout)

    original_create = InterSessionMessageManager.create_message

    def flaky_create(
        manager: InterSessionMessageManager, from_session: str, to_session: str, content: str
    ) -> object:
        if to_session == case.claimant:
            raise psycopg.OperationalError("hub unavailable")
        return original_create(manager, from_session, to_session, content)

    with _intercept_fast_forward(moved_then_timeout):
        timed_out_result = await case.land(case.reviewed(timed_out, "Timed out"), timed_out)
    receipt_task = case.reviewed(receipt, "Receipt write fails")
    with patch.object(
        land_commit, "record_close_receipt", side_effect=psycopg.OperationalError("hub down")
    ):
        receipt_result = await case.land(receipt_task, receipt)
    with patch.object(InterSessionMessageManager, "create_message", flaky_create):
        message_result = await case.land(case.reviewed(message, "Message fails"), message)
    with _intercept_fast_forward(advance_after):
        advanced_result = await case.land(case.reviewed(advanced, "Advanced"), advanced)
    with _intercept_fast_forward(advance_then_timeout):
        late_result = await case.land(case.reviewed(late, "Late"), late)
    with _intercept_fast_forward(interrupted):
        idle_result = await case.land(case.reviewed(idle, "Interrupted"), idle)

    assert timeouts == [120.0]
    assert timed_out_result["landed"] is True, timed_out_result
    assert (receipt_result["landed"], receipt_result["receipt_pending"]) == (True, True)
    assert "receipt_id" not in receipt_result
    assert [r for r in list_close_receipts(case.db, receipt_task.id) if r.kind == LANDING] == []
    assert message_result["landed"] is True
    assert message_result["notification_pending"] == [case.claimant]
    assert (advanced_result["landed_tip"], advanced_result["observed_tip"]) == (advanced, ahead)
    assert (late_result["landed"], late_result["landed_tip"], late_result["observed_tip"]) == (
        True,
        late,
        later,
    )
    assert idle_result["error"] == "git_interrupted"
    assert "?? notes.txt" in idle_result["message"]
    assert " M README.md" in idle_result["message"]
    assert case.git("rev-parse", "trunk") == later
    assert (case.repo / "README.md").read_text() == "dirty\n"
    assert (case.repo / "notes.txt").read_text() == "untracked\n"


async def test_merge_landing_replay_preserves_retest_obligation(case: LandingCase) -> None:
    lane = case.candidate("lane", {"tests/test_lane.py": "lane = 1\n"})
    case.direct({"docs/moved.md": "moved\n"})
    task = case.reviewed(lane, "Merge landing")
    first = await case.land(task, lane)
    replay = await case.land(task, lane)

    pending_lane = case.candidate("pending", {"tests/test_pending.py": "p = 1\n"})
    case.direct({"docs/moved-2.md": "moved\n"})
    pending_task = case.reviewed(pending_lane, "Receipt pending")
    with patch.object(
        land_commit, "record_close_receipt", side_effect=IndeterminateCommitError("unobserved")
    ):
        pending = await case.land(pending_task, pending_lane)
    retried = await case.land(pending_task, pending_lane)

    stacked_a = case.candidate("stack-a", {"web/a.ts": "a\n"})
    stacked_b = case.candidate("stack-b", {"src/gobby/b.py": "b = 1\n"}, base=stacked_a)
    case.direct({"docs/moved-3.md": "moved\n"})
    task_b = case.reviewed(stacked_b, "Stacked B")
    record_close_receipt(
        case.db,
        task=task_b,
        author_session_id=case.creator,
        kind=LANDING_APPROVAL,
        commit_sha=stacked_b,
        facts={"reason": "restart"},
    )
    merged_b = await case.land(task_b, stacked_b)
    merged_a = await case.land(case.reviewed(stacked_a, "Stacked A"), stacked_a)

    ff_a = case.candidate("ff-a", {"web/ff.ts": "a\n"}, base=case.git("rev-parse", "trunk"))
    ff_b = case.candidate("ff-b", {"docs/ff.md": "b\n"}, base=ff_a)
    landed_ff_b = await case.land(case.reviewed(ff_b, "Fast-forward B"), ff_b)
    landed_ff_a = await case.land(case.reviewed(ff_a, "Fast-forward A"), ff_a)

    foreign_a = case.candidate("foreign-a", {"docs/fa.md": "a\n"}, base=ff_b)
    foreign_b = case.candidate("foreign-b", {"docs/fb.md": "b\n"}, base=foreign_a)

    async def foreign(proceed: Proceed, args: Sequence[str], timeout: float) -> GitResult:
        case.git("merge", "--ff-only", foreign_a)
        return await proceed()

    with _intercept_fast_forward(foreign):
        landed_foreign_b = await case.land(case.reviewed(foreign_b, "Foreign B"), foreign_b)
    landed_foreign_a = await case.land(case.reviewed(foreign_a, "Foreign A"), foreign_a)

    assert first["mode"] == "merge"
    assert replay == first
    assert replay["retest_required"] is True
    assert "focused verification commands" in replay["retest_procedure"]
    assert pending["receipt_pending"] is True
    assert (retried["mode"], retried["landed_tip"], retried["activation_class"]) == (
        "merge",
        pending["landed_tip"],
        pending["activation_class"],
    )
    assert (retried["provenance"], retried["retest_required"]) == ("reflog", True)
    assert (merged_b["mode"], merged_b["activation_class"]) == ("merge", "restart")
    assert (merged_a["mode"], merged_a["retest_required"], merged_a["activation_class"]) == (
        "merge",
        True,
        "ui_build",
    )
    assert merged_a["merge_commit"] == merged_b["merge_commit"]
    assert (landed_ff_b["mode"], landed_ff_a["mode"]) == ("ff", "ff")
    assert (landed_ff_a["retest_required"], landed_ff_a["activation_class"]) == (False, "ui_build")
    assert (landed_foreign_b["mode"], landed_foreign_a["provenance"]) == ("ff", "unknown")
    assert landed_foreign_a["mode"] == "already_landed"
