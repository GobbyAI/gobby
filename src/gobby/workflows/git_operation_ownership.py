"""Bind conflict-resolution exemptions to the Git call that started the operation."""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from gobby.hooks.checkout_reads import event_machine_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.checkout_fingerprint import path_fingerprints
from gobby.workflows.commit_guard_conflicts import ForeignPathOwner
from gobby.workflows.task_claim_state import task_checkout_edit_times, task_edit_path_covers
from gobby.workflows.worktree_shell_attribution import (
    background_output_state,
    overlapped_worktree_paths,
    worktree_digests,
)

OPERATIONS_VARIABLE = "git_operation_ownership"
_Result = TypeVar("_Result")


class GitOperationInspectionError(ValueError):
    """Operation provenance could not be inspected safely."""


async def operation_identity(root: str) -> str | None:
    """Identify this checkout's active operation, including its filesystem incarnation."""
    result = await daemon_git.run(["rev-parse", "--absolute-git-dir"], cwd=root, timeout=10.0)
    if not isinstance(result, GitOk):
        raise GitOperationInspectionError(
            f"Git operation inspection failed in {root}: {result.stderr.strip() or result.status}"
        )
    git_dir = Path(result.stdout.strip())
    # Rebase directories survive individual picked commits; REBASE_HEAD does not prove a rebase.
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        path = git_dir / name
        try:
            stat = path.stat()
            content = "" if path.is_dir() else path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GitOperationInspectionError(
                f"Cannot inspect Git operation marker {path}: {exc}"
            ) from exc
        # Directory ctime changes during continuation. The completed call rebinds that identity.
        return json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_ctime_ns, content])
    return None


def _entries(variables: Mapping[str, Any]) -> dict[str, Any]:
    value = variables.get(OPERATIONS_VARIABLE)
    return dict(value) if isinstance(value, dict) else {}


def cancel_operation_calls(variables: dict[str, Any], call_ids: set[str]) -> bool:
    """Release rejected or abandoned calls, restoring a prior continuation's proof."""
    entries = _entries(variables)
    changed = False
    for root, entry in list(entries.items()):
        if (
            not isinstance(entry, dict)
            or entry.get("call_id") not in call_ids
            or not entry.get("pending")
        ):
            continue
        previous = entry.get("previous")
        if isinstance(previous, dict):
            entries[root] = previous
        else:
            entries.pop(root)
        changed = True
    if changed:
        variables[OPERATIONS_VARIABLE] = entries
    return changed


def retire_operation_calls(variables: dict[str, Any], live_call_ids: set[str]) -> bool:
    """Retire reservations alongside the tool bindings that own their lifetime."""
    abandoned = {
        str(entry["call_id"])
        for entry in _entries(variables).values()
        if isinstance(entry, dict) and entry.get("call_id") not in live_call_ids
    }
    return cancel_operation_calls(variables, abandoned)


def latest_edit_time(variables: dict[str, Any], root: str, path: str) -> float | None:
    """Read the latest task or unclaimed edit without trusting a historical holder alone."""
    times: list[float] = []
    tasks = variables.get("task_edited_checkout_times")
    for task in tasks if isinstance(tasks, dict) else ():
        times.extend(
            stamp
            for held, stamp in task_checkout_edit_times(variables, task, root).items()
            if task_edit_path_covers(held, path) or task_edit_path_covers(path, held)
        )
    unclaimed = variables.get("session_unclaimed_edits")
    entries = unclaimed.get(root) if isinstance(unclaimed, dict) else None
    for held, entry in entries.items() if isinstance(entries, dict) else ():
        if not isinstance(entry, dict) or not (
            task_edit_path_covers(held, path) or task_edit_path_covers(path, held)
        ):
            continue
        stamp = entry.get("edited_at")
        if isinstance(stamp, int | float) and not isinstance(stamp, bool):
            times.append(float(stamp))
    return max(times, default=None)


async def operation_exemptions(
    variables: Mapping[str, Any],
    root: str,
    paths: set[str],
    owners: Mapping[str, tuple[ForeignPathOwner, ...]],
) -> tuple[set[str], set[str]]:
    """Exempt the operation's observed bytes; newer foreign writes always remain protected."""
    proof = await owned_operation(variables, root)
    if proof is None:
        return set(), set()
    started, hashes = proof
    protected = set(overlapped_worktree_paths(variables, root)) | {
        path
        for path in paths
        if any(
            (task_edit_path_covers(held, path) or task_edit_path_covers(path, held))
            and any(owner.edited_at is None or owner.edited_at >= started for owner in holders)
            for held, holders in owners.items()
        )
    }
    candidates = paths & hashes.keys() - protected
    current = await asyncio.to_thread(path_fingerprints, Path(root), candidates)
    return {
        path for path in candidates if path in current and current[path] == hashes[path]
    }, protected


async def owned_operation(
    variables: Mapping[str, Any], root: str
) -> tuple[float, dict[str, str | None]] | None:
    """Return provenance only while the exact completed operation remains active."""
    entry = _entries(variables).get(root)
    if not isinstance(entry, dict) or entry.get("pending"):
        return None
    started = entry.get("started_at")
    if not isinstance(started, int | float) or isinstance(started, bool):
        return None
    identity = await operation_identity(root)
    if identity is None or identity != entry.get("identity"):
        return None
    hashes = entry.get("hashes")
    if not isinstance(hashes, dict):
        return None
    return float(started), {
        path: digest
        for path, digest in hashes.items()
        if isinstance(path, str) and (digest is None or isinstance(digest, str))
    }


class GitOperationRegistry(ABC):
    """Persist operation provenance in the same atomic session store as edit ownership."""

    db: HubDatabase

    @abstractmethod
    def _mutate_variables(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[_Result, bool]],
        *,
        keys: Iterable[str],
        apply_defaults: bool = False,
    ) -> _Result: ...

    @abstractmethod
    def get_variable_subset(self, session_id: str, keys: Iterable[str]) -> dict[str, Any]: ...

    async def begin_git_operation_call(
        self, session_id: str, call_id: str, roots: list[str]
    ) -> bool:
        """Sample the start before Git can create markers or alter the worktree."""
        if not roots:
            return True
        started = time.time()
        identities = {root: await operation_identity(root) for root in roots}
        before_hashes: dict[str, dict[str, str | None]] = {}
        for root in roots:
            hashes = await worktree_digests(root)
            if hashes is None:
                raise GitOperationInspectionError(
                    f"Cannot inspect Git operation input paths in {root}."
                )
            before_hashes[root] = hashes

        def begin(variables: dict[str, Any]) -> tuple[None, bool]:
            entries = _entries(variables)
            for root, identity in identities.items():
                previous = entries.get(root)
                if isinstance(previous, dict) and previous.get("call_id") == call_id:
                    continue
                if isinstance(previous, dict) and previous.get("pending"):
                    raise ValueError(f"A Git operation call is still running in {root}.")
                continuing = (
                    isinstance(previous, dict)
                    and not previous.get("pending")
                    and identity is not None
                    and identity == previous.get("identity")
                )
                entries[root] = {
                    "call_id": call_id,
                    "pending": True,
                    "started_at": previous["started_at"]
                    if continuing and isinstance(previous, dict)
                    else started,
                    "before": identity,
                    "before_hashes": before_hashes[root],
                    "continuing": continuing,
                    "previous": previous if continuing else None,
                }
            variables[OPERATIONS_VARIABLE] = entries
            return None, True

        await asyncio.to_thread(
            self._mutate_variables, session_id, begin, keys=(OPERATIONS_VARIABLE,)
        )
        # Publish before checking peers: concurrent contenders cannot both miss each other.
        for root, identity in identities.items():
            rows = await asyncio.to_thread(
                self.db.fetchall,
                "SELECT (v.variables -> %s -> %s)::text AS operation "
                "FROM session_variables v JOIN sessions s ON s.id = v.session_id "
                "WHERE v.session_id <> %s AND s.status = ANY(%s) AND s.machine_id = %s",
                (
                    OPERATIONS_VARIABLE,
                    root,
                    session_id,
                    list(LIVE_SESSION_STATUS_ORDER),
                    event_machine_id(),
                ),
            )
            for row in rows:
                other = json.loads(row["operation"]) if row["operation"] else None
                if isinstance(other, dict) and (
                    other.get("pending")
                    or (identity is not None and other.get("identity") == identity)
                ):
                    await asyncio.to_thread(self.cancel_git_operation_call, session_id, call_id)
                    raise ValueError(f"Another live session owns the Git operation in {root}.")
        return True

    def cancel_git_operation_call(self, session_id: str, call_id: str) -> None:
        """Undo a before-call reservation when a workflow refuses execution."""

        def cancel(variables: dict[str, Any]) -> tuple[None, bool]:
            return None, cancel_operation_calls(variables, {call_id})

        self._mutate_variables(session_id, cancel, keys=(OPERATIONS_VARIABLE,))

    def defer_git_operation_call(self, session_id: str, call_id: str, output_path: str) -> None:
        """Keep a Claude background call pending until its output carries a terminal receipt."""

        def defer(variables: dict[str, Any]) -> tuple[None, bool]:
            entries = _entries(variables)
            changed = False
            for entry in entries.values():
                if (
                    isinstance(entry, dict)
                    and entry.get("call_id") == call_id
                    and entry.get("pending")
                ):
                    entry["output_path"] = output_path
                    changed = True
            if changed:
                variables[OPERATIONS_VARIABLE] = entries
            return None, changed

        self._mutate_variables(session_id, defer, keys=(OPERATIONS_VARIABLE,))

    async def settle_background_git_operations(self, session_id: str) -> None:
        """Settle completed output receipts before guards read operation provenance."""
        variables = await asyncio.to_thread(
            self.get_variable_subset, session_id, (OPERATIONS_VARIABLE,)
        )
        for entry in _entries(variables).values():
            if not isinstance(entry, dict) or not entry.get("pending"):
                continue
            output = entry.get("output_path")
            call_id = entry.get("call_id")
            if not isinstance(output, str) or not isinstance(call_id, str):
                continue
            state = await asyncio.to_thread(background_output_state, output)
            if state == "finished":
                await self.finish_git_operation_call(session_id, call_id)
            elif state == "missing":
                await asyncio.to_thread(self.cancel_git_operation_call, session_id, call_id)

    async def finish_git_operation_call(self, session_id: str, call_id: str) -> None:
        """Bind terminal receipts, including conflict exits, to the resulting Git state."""
        variables = await asyncio.to_thread(
            self.get_variable_subset, session_id, (OPERATIONS_VARIABLE,)
        )
        for root, entry in _entries(variables).items():
            if (
                not isinstance(entry, dict)
                or entry.get("call_id") != call_id
                or entry.get("pending") is not True
            ):
                continue
            identity = await operation_identity(root)
            proven = identity is not None and (
                entry.get("before") is None or entry.get("continuing")
            )
            hashes: dict[str, str | None] | None = None
            if proven:
                hashes = await worktree_digests(root)
                before = entry.get("before_hashes", {})
                previous = entry.get("previous")
                previous_hashes = previous.get("hashes", {}) if isinstance(previous, dict) else {}
                if hashes is not None:
                    # A failed or read-only continuation cannot adopt existing unproven dirt.
                    hashes = {
                        path: digest
                        for path, digest in hashes.items()
                        if path not in before
                        or digest != before[path]
                        or (path in previous_hashes and before[path] == previous_hashes[path])
                    }

            def finish(
                current: dict[str, Any],
                *,
                root: str = root,
                entry: dict[str, Any] = entry,
                identity: str | None = identity,
                hashes: dict[str, str | None] | None = hashes,
            ) -> tuple[None, bool]:
                entries = _entries(current)
                if entries.get(root) != entry:
                    return None, False
                if hashes is None:
                    entries.pop(root, None)
                else:
                    entries[root] = {
                        "call_id": call_id,
                        "started_at": entry["started_at"],
                        "identity": identity,
                        "hashes": hashes,
                    }
                current[OPERATIONS_VARIABLE] = entries
                return None, True

            await asyncio.to_thread(
                self._mutate_variables, session_id, finish, keys=(OPERATIONS_VARIABLE,)
            )
