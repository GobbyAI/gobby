"""Land an independently reviewed task candidate into its main checkout."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_receipts import (
    CLOSE_RECEIPT_AUTHOR_TYPE,
    INDEPENDENT_REVIEW_APPROVAL,
    LANDING,
    LANDING_APPROVAL,
    CloseReceipt,
    list_close_receipts,
    record_close_receipt,
)
from gobby.tasks.landing_policy import ACTIVATION_CLASSES, classify_paths, read_freeze
from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git
from gobby.utils.git import git_subprocess_env
from gobby.utils.machine_id import require_machine_id

__all__ = ["LandingResult", "land_candidate"]

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_LANDING_ACTION = re.compile(
    r"gobby-land candidate=(?P<candidate>[0-9a-f]{40}) "
    rf"mode=(?P<mode>ff|merge) class=(?P<class>{'|'.join(ACTIVATION_CLASSES)})(?:: .*)?"
)


class Overlap(TypedDict):
    task_ref: str
    commit_sha: str
    shared_paths: list[str]


class LandingResult(TypedDict, total=False):
    landed: bool
    error: str
    message: str
    blockers: list[str]
    missing_approvals: list[str]
    overlaps: list[Overlap]
    commit_sha: str
    branch: str
    landed_tip: str
    observed_tip: str
    mode: str
    activation_class: str
    retest_required: bool
    provenance: str
    merge_commit: str
    receipt_id: str


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


async def _paths(main: Path, base: str, sha: str) -> set[str]:
    result = await daemon_git.run(
        ("diff", "-z", "--name-only", "--no-renames", base, sha), cwd=main
    )
    if not isinstance(result, GitOk):
        raise LandingGitError(f"could not read candidate paths: {result.stderr}")
    return {path for path in result.stdout.split("\0") if path}


async def _overlaps(
    db: HubDatabase, task: Task, main: Path, tip: str, sha: str, paths: set[str]
) -> list[Overlap]:
    rows = db.fetchall(
        """
        SELECT DISTINCT t.id::text AS id, t.seq_num
        FROM tasks t JOIN task_comments c ON c.task_id = t.id
        WHERE t.project_id = %s AND t.id <> %s AND t.closed_at IS NULL
          AND c.author_type = %s
        ORDER BY t.seq_num
        """,
        (task.project_id, task.id, CLOSE_RECEIPT_AUTHOR_TYPE),
    )
    overlaps: list[Overlap] = []
    for row in rows:
        candidates = {
            receipt.commit_sha
            for receipt in list_close_receipts(db, row["id"])
            if receipt.kind == INDEPENDENT_REVIEW_APPROVAL
        }
        for other in sorted(candidates):
            if await _resolve(main, other) is None:
                continue
            if (
                await _ancestor(main, other, tip)
                or await _ancestor(main, other, sha)
                or await _ancestor(main, sha, other)
            ):
                continue
            base = await _git(main, "merge-base", tip, other)
            shared = sorted(paths & await _paths(main, base, other))
            if shared:
                overlaps.append(
                    {"task_ref": f"#{row['seq_num']}", "commit_sha": other, "shared_paths": shared}
                )
    return overlaps


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
        "retest_required": True,
        "provenance": "unknown",
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
            if action["candidate"] != sha:
                base = await _git(main, "merge-base", entry.old, sha)
                activation_class = classify_paths(await _paths(main, base, sha))
            facts: dict[str, str | int | bool] = {
                **unknown,
                "landed_tip": entry.new,
                "mode": action["mode"],
                "activation_class": activation_class,
                "retest_required": action["mode"] == "merge",
                "provenance": "reflog",
            }
            if action["mode"] == "merge":
                facts["merge_commit"] = entry.new
            return facts
    except (OSError, UnicodeError, LandingGitError):
        return unknown
    return unknown


def _receipt_result(receipt: CloseReceipt) -> LandingResult:
    facts = receipt.facts
    result: LandingResult = {
        "landed": True,
        "receipt_id": receipt.id,
        "commit_sha": receipt.commit_sha,
        "branch": str(facts["branch"]),
        "landed_tip": str(facts["landed_tip"]),
        "observed_tip": str(facts["observed_tip"]),
        "mode": str(facts["mode"]),
        "activation_class": str(facts["activation_class"]),
        "retest_required": bool(facts["retest_required"]),
        "provenance": str(facts["provenance"]),
    }
    if "merge_commit" in facts:
        result["merge_commit"] = str(facts["merge_commit"])
    return result


async def _record_and_notify(
    db: HubDatabase, task: Task, caller: str, sha: str, facts: dict[str, str | int | bool]
) -> LandingResult:
    receipt, created = record_close_receipt(
        db, task=task, author_session_id=caller, kind=LANDING, commit_sha=sha, facts=facts
    )
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
        for recipient in sorted(recipients):
            manager.create_message(caller, recipient, content)
    return _receipt_result(receipt)


async def land_candidate(
    db: HubDatabase, *, task: Task, caller_session_id: str, commit_sha: str
) -> LandingResult:
    """Land a linked candidate approved by this caller, serialized with project freezes."""
    if not caller_session_id:
        return {"landed": False, "error": "session_required"}
    sha = commit_sha.strip().lower()
    if not _FULL_SHA.fullmatch(sha):
        return {"landed": False, "error": "invalid_commit_sha"}
    repo = Path(require_root(db, task.project_id, require_machine_id()))
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
            tip = await _git(main, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
            receipts = list_close_receipts(db, task.id)
            blockers: list[str] = []
            if current_task.claimed_by_session_id == caller_session_id:
                blockers.append("caller_is_claimant")
            if not any(
                receipt.kind == INDEPENDENT_REVIEW_APPROVAL
                and receipt.author_session_id == caller_session_id
                and receipt.commit_sha == sha
                for receipt in receipts
            ):
                blockers.append("review_receipt_missing")
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
                for receipt in receipts:
                    if (
                        receipt.kind == LANDING
                        and receipt.author_session_id == caller_session_id
                        and receipt.commit_sha == sha
                    ):
                        return _receipt_result(receipt)
                facts = await _recover_landing(main, common, branch, tip, sha)
                return await _record_and_notify(db, current_task, caller_session_id, sha, facts)
            base = await _git(main, "merge-base", tip, sha)
            paths = await _paths(main, base, sha)
            activation_class = classify_paths(paths)
            freeze = await asyncio.to_thread(read_freeze, common)
            overlaps = await _overlaps(db, current_task, main, tip, sha, paths)
            required: set[str] = set()
            if freeze.on:
                required.add("freeze")
            if activation_class in {"restart", "cutover"}:
                required.add("restart")
            if overlaps:
                required.add("overlap")
            granted: set[str] = set()
            for receipt in receipts:
                if receipt.kind == LANDING_APPROVAL and receipt.commit_sha == sha:
                    granted.update(part.strip() for part in str(receipt.facts["reason"]).split(","))
            missing = sorted(required - granted)
            if missing:
                blockers.append("landing_approval_missing")
            if not await _ancestor(main, tip, sha):
                blockers.append("tip_moved")
            refusal: LandingResult = {
                "landed": False,
                "commit_sha": sha,
                "activation_class": activation_class,
                "missing_approvals": missing,
                "overlaps": overlaps,
                "blockers": blockers,
            }
            if blockers:
                refusal["error"] = blockers[0]
                return refusal
            if await _branch(main) != branch:
                refusal["error"] = "main_checkout_branch_changed"
                refusal["blockers"] = ["main_checkout_branch_changed"]
                return refusal
            env = {
                **(git_subprocess_env() or os.environ),
                "GOBBY_LAND_COMMIT": "1",
                "GIT_REFLOG_ACTION": f"gobby-land candidate={sha} mode=ff class={activation_class}",
            }
            result = await daemon_git.run(("merge", "--ff-only", sha), cwd=main, env=env)
            observed_tip = await _git(
                main, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
            )
            if not await _ancestor(main, sha, observed_tip):
                return {**refusal, "error": "git_failed", "message": result.stderr}
            facts = {
                "branch": branch,
                "landed_tip": sha,
                "observed_tip": observed_tip,
                "mode": "ff",
                "activation_class": activation_class,
                "retest_required": False,
                "provenance": "recorded",
            }
            return await _record_and_notify(db, current_task, caller_session_id, sha, facts)
    except LandingGitError as exc:
        return {"landed": False, "error": "git_failed", "message": str(exc)}
