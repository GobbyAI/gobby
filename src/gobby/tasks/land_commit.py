"""The task's Dev Manager lands an independently reviewed candidate into its main checkout."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import psycopg

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.code_index.maintenance_launch import open_launch_async
from gobby.releases.activation import project_checkout_root
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_tasks import claimant_sessions
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskAlreadyEscalatedError
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.tasks.close_receipts import (
    LANDING,
    CloseReceipt,
    list_close_receipts,
    record_close_receipt,
)
from gobby.tasks.landing_policy import (
    ACTIVATION_CLASSES,
    classify_paths,
    missing_schema_carriers,
    read_freeze,
    requires_project_sync,
)
from gobby.tasks.lifecycle_access import is_operator_coordinator
from gobby.tasks.step_records import (
    TaskStepRecord,
    independent_review_approval,
    list_task_step_records,
)
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git
from gobby.utils.git import git_subprocess_env
from gobby.workflows.agent_resolver import session_agent_definition

__all__ = ["LandingResult", "land_candidate", "main_checkout_target"]

logger = logging.getLogger(__name__)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_LANDING_ACTION = re.compile(
    r"gobby-land candidate=(?P<candidate>[0-9a-f]{40}) "
    rf"mode=(?P<mode>ff|merge) class=(?P<class>{'|'.join(ACTIVATION_CLASSES)})"
    r"(?: lander=(?P<lander>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}))?(?:: .*)?"
)
_ATTEMPTS = 3
_LANDING_TIMEOUT = 120.0
_SECURITY_REVIEW_LABEL = "security-review"
# Conflicts go to Triage; every other blocker is the caller's to fix.
_CONFLICTS = frozenset({"base_update_required", "merge_conflict"})


class LandingResult(TypedDict, total=False):
    landed: bool
    already_landed: bool
    landed_by_session_id: str
    error: str
    message: str
    blockers: list[str]
    escalated: bool
    missing_schema_carriers: list[str]
    shared_paths: list[str]
    commit_sha: str
    branch: str
    landed_tip: str
    observed_tip: str
    mode: str
    activation_class: str
    project_sync_required: bool
    retest_required: bool
    provenance: str
    merge_commit: str
    receipt_id: str
    receipt_pending: bool
    notification_pending: list[str]
    worktree_id: str
    worktree_merged: bool


class LandingGitError(RuntimeError):
    """A Git read or write failed before a landing could be proved."""


@dataclass(frozen=True)
class _ReflogEntry:
    old: str
    new: str
    message: str


async def _git(cwd: Path, *args: str) -> str:
    result = await daemon_git.run(args, cwd=cwd)
    if not isinstance(result, GitOk):
        raise LandingGitError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


async def _resolve(cwd: Path, sha: str) -> str | None:
    result = await daemon_git.run(("rev-parse", "--verify", f"{sha}^{{commit}}"), cwd=cwd)
    return result.stdout.strip() if isinstance(result, GitOk) else None


async def _ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    result = await daemon_git.run(("merge-base", "--is-ancestor", ancestor, descendant), cwd=cwd)
    if isinstance(result, GitOk):
        return True
    if isinstance(result, GitFailed) and result.returncode == 1:
        return False
    raise LandingGitError(f"could not compare {ancestor} and {descendant}: {result.stderr}")


async def _branch(main: Path) -> str | None:
    result = await daemon_git.run(("symbolic-ref", "--quiet", "HEAD"), cwd=main)
    if not isinstance(result, GitOk):
        if isinstance(result, GitFailed) and result.returncode == 1:
            return None
        raise LandingGitError(f"could not read main checkout branch: {result.stderr}")
    ref = result.stdout.strip()
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None


async def main_checkout_target(db: HubDatabase, project_id: str) -> tuple[Path, str] | None:
    """Read the registered main checkout and the branch protected by landing."""
    repo = Path(await asyncio.to_thread(project_checkout_root, db, project_id))
    common = Path(await _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    main = common.parent
    branch = await _branch(main)
    return (main, branch) if branch is not None else None


async def _tip(main: Path, branch: str) -> str:
    return await _git(main, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")


async def _paths(main: Path, base: str, sha: str) -> set[str]:
    result = await daemon_git.run(
        ("diff", "-z", "--name-only", "--no-renames", base, sha), cwd=main
    )
    if not isinstance(result, GitOk):
        raise LandingGitError(f"could not read candidate paths: {result.stderr}")
    return {path for path in result.stdout.split("\0") if path}


async def _merge_tree(main: Path, tip: str, sha: str) -> GitOk | GitFailed:
    """Use Git's virtual merge base, retaining conflicted trees for path policy checks."""
    result = await daemon_git.run(("merge-tree", "--write-tree", tip, sha), cwd=main)
    if isinstance(result, GitOk) or (isinstance(result, GitFailed) and result.returncode == 1):
        return result
    raise LandingGitError(f"could not compute candidate merge tree: {result.stderr}")


async def _candidate_paths(main: Path, tip: str, sha: str, tree: str) -> set[str]:
    # Conflicted trees can relocate files to synthesized names absent from both tips.
    return await _paths(main, tip, tree) & await _paths(main, tip, sha)


def _reflog_entries(text: str, oid_length: int) -> list[_ReflogEntry] | None:
    oid = rf"[0-9a-f]{{{oid_length}}}"
    grammar = re.compile(rf"({oid}) ({oid}) .+ <[^<>]*> [0-9]+ [+-][0-9]{{4}}\t(.*)")
    entries: list[_ReflogEntry] = []
    for line in text.splitlines():
        match = grammar.fullmatch(line)
        if match is None:
            return None
        entries.append(_ReflogEntry(*match.groups()))
    return entries


async def _recover_landing(
    main: Path, common: Path, branch: str, tip: str, sha: str
) -> dict[str, str | int | bool]:
    unknown: dict[str, str | int | bool] = {
        "branch": branch,
        "landed_tip": tip,
        "observed_tip": tip,
        "mode": "already_landed",
        "activation_class": "unknown",
        "provenance": "unknown",
        "landed_by_session_id": "unknown",
    }
    try:
        if await _git(main, "rev-parse", "--show-ref-format") != "files":
            return unknown
        text = await asyncio.to_thread(
            (common / "logs" / "refs" / "heads" / branch).read_text, encoding="utf-8"
        )
        entries = _reflog_entries(text, len(tip))
        if entries is None:
            return unknown
        for entry in entries:
            if not entry.message.startswith("gobby-land "):
                continue
            action = _LANDING_ACTION.fullmatch(entry.message)
            if action is None:
                return unknown
            if await _resolve(main, action["candidate"]) is None:
                return unknown
            if not await _ancestor(main, entry.new, tip) or not await _ancestor(
                main, sha, entry.new
            ):
                continue
            if set(entry.old) == {"0"} or await _resolve(main, entry.old) is None:
                return unknown
            if await _ancestor(main, sha, entry.old):
                continue
            activation_class = action["class"]
            facts: dict[str, str | int | bool] = {
                **unknown,
                "landed_tip": entry.new,
                "mode": action["mode"],
                "activation_class": activation_class,
                "provenance": "reflog",
                "landed_by_session_id": (action["lander"] if action["candidate"] == sha else None)
                or "unknown",
            }
            if action["mode"] == "merge":
                facts["merge_commit"] = entry.new
            try:
                merged = await _merge_tree(main, entry.old, sha)
                tree = merged.stdout.split("\n", 1)[0]
                paths = await _candidate_paths(main, entry.old, sha, tree)
            except LandingGitError:
                # Exact-candidate trailers still prove activation and landing provenance.
                # Missing sync facts hold release until project sync is verified.
                return facts if action["candidate"] == sha else unknown
            if action["candidate"] != sha:
                facts["activation_class"] = classify_paths(paths)
            facts["project_sync_required"] = requires_project_sync(paths)
            return facts
    except (OSError, UnicodeError, LandingGitError):
        return unknown
    return unknown


def _facts_result(sha: str, facts: dict[str, str | int | bool]) -> LandingResult:
    result: LandingResult = {
        "landed": True,
        "commit_sha": sha,
        "branch": str(facts["branch"]),
        "landed_tip": str(facts["landed_tip"]),
        "observed_tip": str(facts["observed_tip"]),
        "mode": str(facts["mode"]),
        "activation_class": str(facts["activation_class"]),
        # A disjoint merge keeps the reviewed tip as an ancestor, so no landing needs a retest.
        "retest_required": False,
        "provenance": str(facts["provenance"]),
    }
    if "merge_commit" in facts:
        result["merge_commit"] = str(facts["merge_commit"])
    if "project_sync_required" in facts:
        result["project_sync_required"] = bool(facts["project_sync_required"])
    return result


def _receipt_result(receipt: CloseReceipt, *, already_landed: bool = True) -> LandingResult:
    return {
        **_facts_result(receipt.commit_sha, receipt.facts),
        "receipt_id": receipt.id,
        "already_landed": already_landed,
        "landed_by_session_id": str(
            receipt.facts.get("landed_by_session_id", receipt.author_session_id)
        ),
    }


async def _record_and_notify(
    db: HubDatabase,
    task: Task,
    caller: str,
    sha: str,
    facts: dict[str, str | int | bool],
    *,
    already_landed: bool = False,
) -> LandingResult:
    try:
        receipt, created = record_close_receipt(
            db, task=task, author_session_id=caller, commit_sha=sha, facts=facts
        )
    except (psycopg.Error, IndeterminateCommitError):
        # The ref already moved; a later call replays the receipt or records it from the reflog.
        return {
            **_facts_result(sha, facts),
            "already_landed": already_landed,
            "landed_by_session_id": str(
                facts.get("landed_by_session_id", "unknown" if already_landed else caller)
            ),
            "receipt_pending": True,
        }
    result = _receipt_result(receipt, already_landed=already_landed or not created)
    if created:
        recipients = {
            recipient
            for recipient in (
                task.claimed_by_session_id,
                task.created_in_session_id,
                task.delegated_by_session_id,
            )
            if recipient is not None and recipient != caller
        }
        content = (
            f'Landed #{task.seq_num} "{task.title}" {sha[:10]} on {facts["branch"]} '
            f"at {str(facts['landed_tip'])[:10]}; activation {facts['activation_class']}"
        )
        if facts.get("project_sync_required"):
            content += "; project workflow sync required"
        manager = InterSessionMessageManager(db)
        unreached: list[str] = []
        for recipient in sorted(recipients):
            try:
                manager.create_message(caller, recipient, content)
            except (psycopg.Error, IndeterminateCommitError):
                unreached.append(recipient)
        if unreached:
            result["notification_pending"] = unreached
    return result


def _refused(refusal: LandingResult, error: str, message: str | None = None) -> LandingResult:
    result: LandingResult = {**refusal, "error": error, "blockers": [error]}
    if message is not None:
        result["message"] = message
    return result


def _manages_task(db: HubDatabase, task: Task, caller_session_id: str) -> bool:
    """A definition grants landing to the task's bound manager or a local root."""
    caller = SessionManager(db).get(caller_session_id)
    if caller is None:
        return False
    run = LocalAgentRunManager(db).get_by_session(caller.id)
    definition = session_agent_definition(db, caller)
    if run is not None and run.task_id == task.id and definition is not None:
        if "land_tasks" in definition[0].capabilities:
            return True
    return is_operator_coordinator(LocalTaskManager(db), caller, capability="land_tasks")


def _review_blockers(
    task: Task, records: list[TaskStepRecord], sha: str, caller: str, claimants: set[str]
) -> list[str]:
    blockers: list[str] = []
    if any(
        record.step == "review"
        and record.epoch == task.review_epoch
        and record.session_id == caller
        for record in records
    ):
        blockers.append("caller_is_reviewer")
    if independent_review_approval(task, records, sha, caller) is None:
        blockers.append("review_approval_missing")
    if _SECURITY_REVIEW_LABEL in (task.labels or []) and not any(
        record.step == "review"
        and record.status == "approved"
        and record.epoch == task.review_epoch
        and record.commit_sha == sha
        and "approve_security_review" in record.agent_capabilities
        and record.session_id not in {caller, *claimants}
        for record in records
    ):
        blockers.append("security_review_missing")
    return blockers


def _escalate(db: HubDatabase, task: Task, refusal: LandingResult) -> LandingResult:
    """Send a conflicting landing to Triage; nothing resolves a conflict by overwriting."""
    reason = {
        "kind": "landing_conflict",
        "commit_sha": refusal["commit_sha"],
        "blockers": refusal["blockers"],
        "shared_paths": refusal.get("shared_paths", []),
    }
    try:
        LocalTaskManager(db).escalate_task(task.id, json.dumps(reason))
    except TaskAlreadyEscalatedError:
        pass
    except ValueError:
        # A closed task has no Triage path left; the refusal stands alone.
        return refusal
    return {**refusal, "escalated": True}


async def _mark_task_worktree(
    db: HubDatabase, task: Task, main: Path, result: LandingResult
) -> LandingResult:
    """Mark the task's tree merged once its HEAD holds the candidate and the tip holds HEAD."""
    worktrees = LocalWorktreeManager(db)
    row = worktrees.get_by_task(task.id)
    if row is None:
        return result
    merged = row.status == WorktreeStatus.MERGED.value
    path = Path(row.worktree_path)
    if not merged and row.status in {WorktreeStatus.ACTIVE.value, WorktreeStatus.STALE.value}:
        try:
            head = await _resolve(path, "HEAD") if path.is_dir() else None
            if (
                head is not None
                and await _ancestor(main, result["commit_sha"], head)
                and await _ancestor(main, head, result["landed_tip"])
            ):
                # Read the branch from Git: the registry label can be stale.
                branch = await _branch(path)
                if branch is not None and branch != row.branch_name:
                    worktrees.update(row.id, branch_name=branch)
                merged = worktrees.mark_merged(row.id, cleanup_days=0) is not None
        except LandingGitError:
            pass  # A tree Git cannot read stays unmarked for the sweeper to judge.
    return {**result, "worktree_id": row.id, "worktree_merged": merged}


async def _refresh_index(
    main: Path,
    sha: str,
    project_id: str,
    paths: set[str],
    code_index: CodeIndexContext | None,
) -> None:
    """Re-index the landed paths in the main checkout; a failure never fails the landing.

    The refresh runs under the same managed launch the code-index trigger uses, read
    when the landing runs because the daemon binds its launch factory after startup.
    """
    if not paths:
        return
    factory = code_index.launch_factory if code_index is not None else None
    try:
        if factory is None:
            result = await GcodeGateway().incremental_index(
                main, sorted(paths), timeout=_LANDING_TIMEOUT
            )
        else:
            async with open_launch_async(
                factory,
                project_id,
                timeout_seconds=_LANDING_TIMEOUT,
                code_overlay_project_id=overlay_project_id_for_root(main),
            ) as launch:
                result = await GcodeGateway().incremental_index(
                    main, sorted(paths), timeout=_LANDING_TIMEOUT, env=launch.env
                )
    except Exception as exc:  # Launch and gateway faults never fail a landed commit.
        logger.warning("Index refresh failed after landing %s: %s", sha, exc)
        return
    # Exit 3 is --skip-if-locked yielding to a concurrent indexer, not a failure.
    if result.returncode not in (0, 3):
        logger.warning(
            "Index refresh exited %s after landing %s: %s", result.returncode, sha, result.stderr
        )


async def land_candidate(
    db: HubDatabase,
    *,
    task: Task,
    caller_session_id: str,
    commit_sha: str,
    code_index: CodeIndexContext | None = None,
) -> LandingResult:
    """Land a linked candidate another session approved, as the task's Dev Manager.

    A tip that moved by paths disjoint from the candidate's lands a generated merge;
    paths the tip changed too and merge conflicts escalate the task to Triage.
    A tip that moves during the call is re-read and every policy check repeats.
    """
    if not caller_session_id:
        return {"landed": False, "error": "session_required"}
    sha = commit_sha.strip().lower()
    if not _FULL_SHA.fullmatch(sha):
        return {"landed": False, "error": "invalid_commit_sha"}
    repo = Path(await asyncio.to_thread(project_checkout_root, db, task.project_id))
    try:
        if await _resolve(repo, sha) != sha:
            return {"landed": False, "error": "candidate_unresolved"}
        common = Path(await _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        main = common.parent
        async with db.advisory_lock(MainCheckoutLanding(task.project_id)):
            current_task = LocalTaskManager(db).get_task(task.id)
            branch = await _branch(main)
            if branch is None:
                return {"landed": False, "error": "main_checkout_detached"}
            linked = False
            for candidate in current_task.commits or []:
                if await _resolve(main, candidate) == sha:
                    linked = True
                    break
            refusal: LandingResult = {"landed": False, "commit_sha": sha}
            for _attempt in range(_ATTEMPTS):
                # Every attempt re-reads the tip, so a candidate already landed counts.
                tip = await _tip(main, branch)
                receipts = list_close_receipts(db, task.id)
                records = list_task_step_records(db, task.id)
                with db.transaction() as conn:
                    claimants = claimant_sessions(conn, task.id)
                blockers: list[str] = []
                if not _manages_task(db, current_task, caller_session_id):
                    blockers.append("caller_not_task_manager")
                if current_task.claimed_by_session_id == caller_session_id:
                    blockers.append("caller_is_claimant")
                elif caller_session_id in claimants:
                    blockers.append("caller_was_claimant")
                blockers += _review_blockers(
                    current_task, records, sha, caller_session_id, claimants
                )
                if not linked:
                    blockers.append("candidate_not_linked")
                if await _ancestor(main, sha, tip):
                    if blockers:
                        return {
                            "landed": False,
                            "error": blockers[0],
                            "blockers": blockers,
                            "commit_sha": sha,
                        }
                    # Landing receipts belong to the task/SHA, regardless of the replay caller.
                    replayed = next(
                        (r for r in receipts if r.kind == LANDING and r.commit_sha == sha), None
                    )
                    if replayed is not None:
                        return await _mark_task_worktree(
                            db, current_task, main, _receipt_result(replayed)
                        )
                    facts = await _recover_landing(main, common, branch, tip, sha)
                    recovered = await _record_and_notify(
                        db, current_task, caller_session_id, sha, facts, already_landed=True
                    )
                    return await _mark_task_worktree(db, current_task, main, recovered)
                merged = await _merge_tree(main, tip, sha)
                tree = merged.stdout.split("\n", 1)[0]
                paths = await _candidate_paths(main, tip, sha, tree)
                shared = sorted(paths & await _paths(main, sha, tree))
                missing_carriers = missing_schema_carriers(paths)
                if missing_carriers:
                    blockers.append("schema_carriers_missing")
                activation_class = classify_paths(paths)
                # A freeze refuses outright; restart-class landings need no extra approval.
                if (await asyncio.to_thread(read_freeze, common)).on:
                    blockers.append("landing_frozen")
                # Only the integration tip counts; unlanded peers never block a landing.
                if shared:
                    blockers.append("base_update_required")
                refusal = {
                    "landed": False,
                    "commit_sha": sha,
                    "activation_class": activation_class,
                    "project_sync_required": requires_project_sync(paths),
                    "shared_paths": shared,
                    "missing_schema_carriers": missing_carriers,
                    "blockers": blockers,
                }
                if blockers:
                    refusal["error"] = blockers[0]
                    if _CONFLICTS.issuperset(blockers):
                        return _escalate(db, current_task, refusal)
                    return refusal
                if await _branch(main) != branch:
                    return _refused(refusal, "main_checkout_branch_changed")
                # Decision Record item 1: a tip moved by disjoint paths lands a generated
                # two-parent merge, which neither the ref nor the checkout sees until the
                # fast-forward below.
                target, mode = sha, "ff"
                if not await _ancestor(main, tip, sha):
                    if isinstance(merged, GitFailed):
                        conflict = _refused(refusal, "merge_conflict", merged.stdout)
                        return _escalate(db, current_task, conflict)
                    subject = f"chore: land reviewed {sha[:10]} for #{current_task.seq_num}"
                    target = await _git(
                        main, "commit-tree", tree, "-p", tip, "-p", sha, "-m", subject
                    )
                    mode = "merge"
                env = {
                    **(git_subprocess_env() or os.environ),
                    # The refusal classes below match git's English stderr.
                    "LC_ALL": "C",
                    "GOBBY_LAND_COMMIT": "1",
                    "GIT_REFLOG_ACTION": (
                        f"gobby-land candidate={sha} mode={mode} class={activation_class} "
                        f"lander={caller_session_id}"
                    ),
                }
                result = await daemon_git.run(
                    ("merge", "--ff-only", target), cwd=main, env=env, timeout=_LANDING_TIMEOUT
                )
                # Read once, whatever git returned: it can move the ref before failing.
                observed_tip = await _tip(main, branch)
                if await _ancestor(main, target, observed_tip):
                    facts = {
                        "branch": branch,
                        "landed_tip": target,
                        "observed_tip": observed_tip,
                        "mode": mode,
                        "activation_class": activation_class,
                        "project_sync_required": requires_project_sync(paths),
                        "provenance": "recorded",
                        "landed_by_session_id": caller_session_id,
                    }
                    if mode == "merge":
                        facts["merge_commit"] = target
                    landed = await _record_and_notify(
                        db, current_task, caller_session_id, sha, facts
                    )
                    await _refresh_index(main, sha, task.project_id, paths, code_index)
                    return await _mark_task_worktree(db, current_task, main, landed)
                if observed_tip != tip:
                    continue
                # Nothing below resets the shared checkout to compensate.
                if isinstance(result, GitTimeout) or (result.returncode or 0) < 0:
                    status = await daemon_git.run(("status", "--porcelain"), cwd=main)
                    return _refused(refusal, "git_interrupted", status.stdout)
                if "would be overwritten" in result.stderr:
                    return _refused(refusal, "checkout_dirty", result.stderr)
                if "index.lock" in result.stderr:
                    return _refused(refusal, "checkout_busy", result.stderr)
                return _refused(refusal, "git_failed", result.stderr)
            return _refused(refusal, "tip_contention")
    except LandingGitError as exc:
        return {"landed": False, "error": "git_failed", "message": str(exc)}
