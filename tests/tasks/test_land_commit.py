"""Landing boundaries exercised with isolated PostgreSQL and throwaway Git repositories."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
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
from gobby.tasks.land_commit import LandingResult, land_candidate
from gobby.tasks.landing_policy import write_freeze
from gobby.utils.daemon_git import GitOk, GitResult, daemon_git
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
