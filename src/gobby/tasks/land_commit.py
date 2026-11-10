"""The task's Dev Team Lead lands an independently reviewed candidate into its main checkout."""

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
from pydantic import JsonValue

from gobby.agents.isolation_git_hygiene import blocking_status_lines
from gobby.code_index.context import CodeIndexContext
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeIndexLock
from gobby.code_index.maintenance_launch import open_launch_async
from gobby.install.bin_release_identity import BinaryReleaseVersionError, unbumped_binaries
from gobby.releases.activation import project_checkout_root
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    Task,
    TaskAlreadyEscalatedError,
    TaskStaleStateError,
)
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.tasks.close_receipts import (
    LANDING,
    CloseReceipt,
    list_close_receipts,
    record_close_receipt,
)
from gobby.tasks.closure_snapshot import prepare_closure
from gobby.tasks.landing_policy import (
    ACTIVATION_CLASSES,
    classify_paths,
    missing_schema_carriers,
    read_freeze,
)
from gobby.tasks.lifecycle_access import (
    CrossProjectTaskForbidden,
    resolve_lifecycle_task,
    task_scope_refusal,
)
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git
from gobby.utils.git import git_subprocess_env
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import session_agent_definition

__all__ = ["LandingResult", "land_candidate", "main_checkout_target"]

logger = logging.getLogger(__name__)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
# Reflogs retain earlier landings after their activation policy changes.
_REFLOG_ACTIVATION_CLASSES = (*ACTIVATION_CLASSES, "sync")
_LANDING_ACTION = re.compile(
    r"gobby-land candidate=(?P<candidate>[0-9a-f]{40}) "
    rf"mode=(?P<mode>ff|merge) class=(?P<class>{'|'.join(_REFLOG_ACTIVATION_CLASSES)})"
    r"(?: lander=(?P<lander>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}))?(?:: .*)?"
)
_ATTEMPTS = 3
_LANDING_TIMEOUT = 120.0
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
    unbumped_binaries: list[str]
    commit_sha: str
    branch: str
    landed_tip: str
    observed_tip: str
    mode: str
    activation_class: str
    admission_basis: str
    admission_policy_revision: str
    admission_snapshot_revision: str
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


async def task_landing_target(db: HubDatabase, task: Task) -> tuple[Path, str] | None:
    """Leaves land in their epic's tree; top-level tasks land in the main checkout."""
    from gobby.agents.team_admission_records import team_binding
    from gobby.agents.team_lifecycle import task_team
    from gobby.storage.team_records import EPIC_MODE

    team = await asyncio.to_thread(task_team, db, task.id)
    epic = None
    if team and team.parent_team_id:
        epic = await asyncio.to_thread(team_binding, db, team.parent_team_id)
        if epic is None:
            raise LandingGitError("the pod's parent team has no admission binding")
    elif team and team.mode == EPIC_MODE and team.task_id != task.id:
        epic = team
    if epic is not None:
        if epic.worktree_path is None or await _branch(Path(epic.worktree_path)) != epic.branch:
            raise LandingGitError("the epic landing worktree is unavailable or changed branch")
        return Path(epic.worktree_path), epic.branch
    return await main_checkout_target(db, task.project_id)


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


async def _unbumped_binaries(main: Path, tip: str, tree: str, paths: set[str]) -> list[str]:
    """Shipped binaries the landed tree changes without a version bump; unreadable fails closed."""
    try:
        return await asyncio.to_thread(unbumped_binaries, main, tip, tree, paths)
    except BinaryReleaseVersionError as exc:
        raise LandingGitError(str(exc)) from exc


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
            activation_class = "restart" if action["class"] == "sync" else action["class"]
            facts: dict[str, str | int | bool] = {
                **unknown,
                "integration_base": entry.old,
                "landed_tip": entry.new,
                "mode": action["mode"],
                "activation_class": activation_class,
                "provenance": "reflog",
                "landed_by_session_id": (action["lander"] if action["candidate"] == sha else None)
                or "unknown",
            }
            if action["mode"] == "merge":
                facts["merge_commit"] = entry.new
            if action["candidate"] == sha:
                return facts
            try:
                merged = await _merge_tree(main, entry.old, sha)
                tree = merged.stdout.split("\n", 1)[0]
                paths = await _candidate_paths(main, entry.old, sha, tree)
            except LandingGitError:
                return unknown
            facts["activation_class"] = classify_paths(paths)
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
        "provenance": str(facts["provenance"]),
    }
    if "merge_commit" in facts:
        result["merge_commit"] = str(facts["merge_commit"])
    if "admission_basis" in facts:
        result["admission_basis"] = str(facts["admission_basis"])
        result["admission_policy_revision"] = str(facts["admission_policy_revision"])
        result["admission_snapshot_revision"] = str(facts["admission_snapshot_revision"])
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
    """The installed definition grants landing within its declared task scope."""
    caller = SessionManager(db).get(caller_session_id)
    if caller is None or caller.machine_id != require_machine_id():
        return False
    try:
        resolve_lifecycle_task(
            LocalTaskManager(db),
            task.id,
            project_id=caller.project_id,
            caller_session_id=caller.id,
            sessions=SessionManager(db),
        )
    except CrossProjectTaskForbidden:
        return False
    definition = session_agent_definition(db, caller)
    if definition is None or "land_tasks" not in definition[0].capabilities:
        return False
    return task_scope_refusal(task, caller.id, definition[0].task_land_scope, db=db) is None


async def candidate_scope_paths(
    main: Path,
    tip: str,
    sha: str,
    receipts: list[CloseReceipt],
    linked_commits: list[str] | None = None,
) -> list[str] | None:
    """Pin the candidate's immutable path scope before definition admission."""
    if linked_commits:
        paths: set[str] = set()
        for linked in {sha, *linked_commits}:
            resolved = await _resolve(main, linked)
            if resolved is None:
                return None
            scope = await candidate_scope_paths(main, tip, resolved, receipts)
            if scope is None:
                return None
            paths.update(scope)
        return sorted(paths)
    base = await _git(main, "merge-base", tip, sha)
    if base == sha:
        # Recheck the original candidate scope, even when the integration delta
        # is now empty. Missing scope provenance cannot authorize a direct replay.
        receipt = next(
            (r for r in receipts if r.commit_sha == sha or r.facts.get("landed_tip") == sha), None
        )
        original_tip = receipt.facts.get("integration_base") if receipt else None
        if not isinstance(original_tip, str) or not _FULL_SHA.fullmatch(original_tip):
            # Operator commits and externally integrated commits need no daemon
            # receipt to prove their immutable first-parent change scope.
            result = await daemon_git.run(
                (
                    "diff-tree",
                    "--root",
                    "--first-parent",
                    "--diff-merges=first-parent",
                    "-r",
                    "--no-renames",
                    "--name-only",
                    "-z",
                    "--no-commit-id",
                    sha,
                ),
                cwd=main,
            )
            if not isinstance(result, GitOk):
                raise LandingGitError(f"could not read committed candidate paths: {result.stderr}")
            return sorted(path for path in result.stdout.split("\0") if path)
        sha = receipt.commit_sha if receipt else sha
        base = await _git(main, "merge-base", original_tip, sha)
    return sorted(await _paths(main, base, sha))


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
        # Waits for a busy index lock: a skipped refresh exits 3 without indexing, and
        # nothing else would index the landed paths before the next maintenance pass.
        if factory is None:
            result = await GcodeGateway().incremental_index(
                main, sorted(paths), timeout=_LANDING_TIMEOUT, lock=GcodeIndexLock.WAIT
            )
        else:
            async with open_launch_async(
                factory,
                project_id,
                timeout_seconds=_LANDING_TIMEOUT,
                code_overlay_project_id=overlay_project_id_for_root(main),
            ) as launch:
                result = await GcodeGateway().incremental_index(
                    main,
                    sorted(paths),
                    timeout=_LANDING_TIMEOUT,
                    env=launch.env,
                    lock=GcodeIndexLock.WAIT,
                )
    except Exception as exc:  # Launch and gateway faults never fail a landed commit.
        logger.warning("Index refresh failed after landing %s: %s", sha, exc)
        _hand_to_trigger(code_index, main, project_id, paths, sha)
        return
    if not result.success:
        logger.warning(
            "Index refresh exited %s after landing %s: %s", result.returncode, sha, result.stderr
        )
        _hand_to_trigger(code_index, main, project_id, paths, sha)


def _hand_to_trigger(
    code_index: CodeIndexContext | None,
    main: Path,
    project_id: str,
    paths: set[str],
    sha: str,
) -> None:
    """Queue the landed paths the refresh did not index; the trigger retries them."""
    trigger = code_index.index_trigger if code_index is not None else None
    if trigger is None:
        logger.warning(
            "Landing %s left %d path(s) unindexed: no code index trigger to retry them",
            sha,
            len(paths),
        )
        return
    root = str(main)
    for path in sorted(paths):
        trigger.notify_file_changed(str(main / path), project_id, root)


async def land_candidate(
    db: HubDatabase,
    *,
    task: Task,
    caller_session_id: str,
    commit_sha: str,
    code_index: CodeIndexContext | None = None,
) -> LandingResult:
    """Land a linked candidate under the caller's declared integration authority.

    A tip that moved by paths disjoint from the candidate's lands a generated merge;
    paths the tip changed too and merge conflicts escalate the task to Triage.
    A tip that moves during the call is re-read and every policy check repeats.
    Each attempt admits one coherent task/definition/evidence snapshot. Later
    policy or evidence changes govern later attempts; receipts retain the exact
    revisions that authorized the admitted request.
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
        async with db.advisory_lock(MainCheckoutLanding(task.project_id)):
            current_task = LocalTaskManager(db).get_task(task.id)
            landing_target = await task_landing_target(db, current_task)
            if landing_target is None:
                return {"landed": False, "error": "main_checkout_detached"}
            main, branch = landing_target
            refusal: LandingResult = {"landed": False, "commit_sha": sha}
            for _attempt in range(_ATTEMPTS):
                # Every attempt re-reads the tip, so a candidate already landed counts.
                tip = await _tip(main, branch)
                receipts = list_close_receipts(db, task.id)
                blockers: list[str] = []
                if not _manages_task(db, current_task, caller_session_id):
                    blockers.append("landing_authority_required")
                grant_paths = await candidate_scope_paths(
                    main, tip, sha, receipts, list(current_task.commits or [])
                )
                admission: dict[str, str | int | bool] = {}
                if not blockers:

                    def derive_scope(
                        _task: Task,
                        agent: AgentDefinitionBody,
                        paths: tuple[str, ...] = tuple(grant_paths or ()),
                    ) -> dict[str, JsonValue]:
                        return {
                            "integration_path_grant": agent.covers_direct_integration_paths(paths)
                        }

                    prepared = prepare_closure(
                        db,
                        task_id=task.id,
                        session_id=caller_session_id,
                        expected_updated_at=current_task.updated_at,
                        event="task_land",
                        facts={
                            "closure_intent": "landing",
                            "candidate_commit_sha": sha,
                            "candidate_paths": list(grant_paths or ()),
                            "candidate_paths_available": grant_paths is not None,
                            "integration_branch": branch,
                            "integration_base": tip,
                            "candidate_contains_integration_base": await _ancestor(main, tip, sha),
                        },
                        derive_facts=derive_scope,
                    )
                    current_task = prepared.task
                    if (
                        "land_tasks" not in prepared.agent.capabilities
                        or prepared.snapshot.caller.get("machine_id") != require_machine_id()
                        or task_scope_refusal(
                            current_task, caller_session_id, prepared.agent.task_land_scope, db=db
                        )
                        is not None
                    ):
                        blockers.append("landing_authority_required")
                    blockers.extend(prepared.decision.blockers)
                    admission = {
                        "admission_basis": "request_snapshot",
                        "admission_policy_revision": prepared.decision.policy_revision,
                        "admission_snapshot_revision": prepared.decision.snapshot_revision,
                    }
                linked = False
                for candidate in current_task.commits or []:
                    if await _resolve(main, candidate) == sha:
                        linked = True
                        break
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
                    facts.update(admission)
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
                # A landed crate change must bump the version of every binary it reaches.
                binaries = (
                    await _unbumped_binaries(main, tip, tree, paths)
                    if isinstance(merged, GitOk)
                    else []
                )
                if binaries:
                    blockers.append("binary_bump_required")
                refusal = {
                    "landed": False,
                    "commit_sha": sha,
                    "activation_class": activation_class,
                    "shared_paths": shared,
                    "missing_schema_carriers": missing_carriers,
                    "blockers": blockers,
                }
                if blockers:
                    refusal["error"] = blockers[0]
                    if binaries:
                        refusal["unbumped_binaries"] = binaries
                        refusal["message"] = (
                            "bump the patch version of each changed shipped binary: "
                            + ", ".join(binaries)
                        )
                    if _CONFLICTS.issuperset(blockers):
                        return _escalate(db, current_task, refusal)
                    return refusal
                if await _branch(main) != branch:
                    return _refused(refusal, "main_checkout_branch_changed")
                if main.resolve() != repo.resolve():
                    status = await daemon_git.run(("status", "--porcelain"), cwd=main)
                    if not isinstance(status, GitOk):
                        return _refused(refusal, "git_failed", status.stderr)
                    if blocking_status_lines(status.stdout, main, repo):
                        return _refused(refusal, "checkout_dirty", status.stdout)
                # Decision Record item 1: a tip moved by disjoint paths lands a generated
                # two-parent merge, which neither the ref nor the checkout sees until the
                # fast-forward below.
                target, mode = sha, "ff"
                if not await _ancestor(main, tip, sha):
                    if isinstance(merged, GitFailed):
                        conflict = _refused(refusal, "merge_conflict", merged.stdout)
                        return _escalate(db, current_task, conflict)
                    subject = f"chore: land {sha[:10]} for #{current_task.seq_num}"
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
                        **admission,
                        "branch": branch,
                        "integration_base": tip,
                        "landed_tip": target,
                        "observed_tip": observed_tip,
                        "mode": mode,
                        "activation_class": activation_class,
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
    except TaskStaleStateError:
        return {"landed": False, "error": "task_changed"}
    except LandingGitError as exc:
        return {"landed": False, "error": "git_failed", "message": str(exc)}
