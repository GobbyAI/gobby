"""Helpers for managing multi-task claim state in session variables.

Sessions can claim N tasks simultaneously. The state is a single dict
``claimed_tasks: {uuid: "#N"}`` mapping task UUIDs to display refs.
``task_claimed`` is True when the dict is non-empty.
"""

from __future__ import annotations

import os
import posixpath
import time
from collections.abc import Collection
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.workflows.checkout_fingerprint import path_fingerprints

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def effective_tool_cwd(event_cwd: str | None, event_data: dict[str, Any]) -> str | None:
    """Resolve the tool's cwd only from an absolute, proven base."""
    base = Path(event_cwd).expanduser() if event_cwd and event_cwd.strip() else None
    if base is not None and not base.is_absolute():
        base = None
    tool_input = event_data.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("workdir", "cwd"):
            override = tool_input.get(key)
            if not isinstance(override, str) or not override.strip():
                continue
            path = Path(override).expanduser()
            if not path.is_absolute():
                if base is None:
                    return None
                path = base / path
            return str(path.resolve(strict=False))
    return str(base.resolve(strict=False)) if base is not None else None


def sanctioned_worktree_root(
    db: HubDatabase, variables: dict[str, Any], root: str, root_project_id: str
) -> bool:
    """Whether a claimed task authorizes this registered project's checkout."""
    from gobby.storage.worktrees import LocalWorktreeManager

    manager = LocalWorktreeManager(db)
    return any(
        os.path.realpath(row.worktree_path) == os.path.realpath(root)
        for task_id in _claimed_tasks(variables)
        for row in manager.list_worktrees(project_id=root_project_id, task_id=task_id)
    )


def resolve_edit_pair(
    file_path: str,
    cwd: str | None,
    *,
    project_id: str | None,
    db: HubDatabase | None,
    variables: dict[str, Any],
) -> tuple[str, str] | None:
    """Resolve the real target to its innermost authorized project checkout."""
    from gobby.hooks._path_scope import checkout_root
    from gobby.hooks.checkout_reads import node_origin
    from gobby.utils.project_context import find_project_root, get_project_context

    if node_origin():
        return _resolve_node_edit_pair(file_path, cwd, project_id=project_id, db=db)

    target = Path(file_path).expanduser()
    if not target.is_absolute():
        if cwd is None:
            return None
        target = Path(cwd) / target
    target = target.resolve(strict=False)
    root = find_project_root(target if target.is_dir() else target.parent)
    if root is None:
        return None
    root = root.resolve(strict=False)
    context = get_project_context(root)
    root_project_id = context.get("id") if context else None
    if root_project_id != project_id:
        if (
            db is None
            or not isinstance(root_project_id, str)
            or not sanctioned_worktree_root(db, variables, str(root), root_project_id)
        ):
            return None
    # A Git checkout nested below the project root owns its own status, so key on it.
    nested = checkout_root(target)
    if nested is not None and nested != root and nested.is_relative_to(root):
        root = nested
    return str(root), os.path.normpath(str(target.relative_to(root)))


def _resolve_node_edit_pair(
    file_path: str, cwd: str | None, *, project_id: str | None, db: HubDatabase | None
) -> tuple[str, str] | None:
    """Attribute node paths without consulting the hub's filesystem."""
    from gobby.hooks.checkout_reads import (
        CheckoutReadUnavailable,
        event_machine_id,
        path_facts,
    )
    from gobby.storage.project_checkouts import LocalProjectCheckoutManager

    try:
        fact = path_facts([file_path], cwd=cwd).files[0]
    except CheckoutReadUnavailable:
        machine_id = event_machine_id()
        if db is None or project_id is None or machine_id is None:
            return None
        checkout = LocalProjectCheckoutManager(db).get(machine_id, project_id)
        if checkout is None:
            return None
        root = Path(checkout.root_path)
        target = Path(file_path)
        if not target.is_absolute():
            if cwd is None:
                return None
            target = Path(cwd) / target
        target = Path(os.path.normpath(target))
    else:
        if fact.kind == "outside" or fact.project_root is None or fact.project_id != project_id:
            return None
        root = Path(fact.project_root)
        target = Path(fact.resolved)
    if not target.is_relative_to(root):
        return None
    return str(root), str(target.relative_to(root))


def normalize_task_edited_path(value: object) -> str | None:
    """Normalize one repository-relative task attribution path."""
    if not isinstance(value, str) or not value:
        return None
    path = posixpath.normpath(value.replace("\\", "/"))
    if path == "" or path == ".." or path.startswith("../") or path.startswith("/"):
        return None
    return path


def normalize_task_checkout_root(value: object) -> str | None:
    """Normalize one absolute checkout root used by task edit attribution."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    return str(path.resolve(strict=False))


def _claimed_tasks(variables: dict[str, Any]) -> dict[str, str]:
    raw = variables.get("claimed_tasks") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(task_id): str(ref) for task_id, ref in raw.items()}


def _task_edited_files(variables: dict[str, Any]) -> dict[str, list[str]]:
    raw = variables.get("task_edited_files") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for task_id, files in raw.items():
        if isinstance(files, list):
            result[str(task_id)] = sorted({str(file) for file in files if file})
    return result


def _task_edited_file_checkouts(
    variables: dict[str, Any],
    ledger_name: str = "task_edited_file_checkouts",
) -> dict[str, dict[str, list[str]]]:
    raw = variables.get(ledger_name) or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, list[str]]] = {}
    for task_id, raw_checkouts in raw.items():
        if not isinstance(raw_checkouts, dict):
            continue
        checkouts: dict[str, list[str]] = {}
        for raw_root, raw_files in raw_checkouts.items():
            root = normalize_task_checkout_root(raw_root)
            if root is None or not isinstance(raw_files, list):
                continue
            checkouts[root] = sorted(
                {
                    path
                    for value in raw_files
                    if (path := normalize_task_edited_path(value)) is not None
                }
            )
        result[str(task_id)] = checkouts
    return result


def add_claimed_task(variables: dict[str, Any], task_id: str, ref: str) -> dict[str, Any]:
    """Return merge dict that adds a task to the claimed set (idempotent)."""
    tasks = _claimed_tasks(variables)
    tasks[task_id] = ref
    return {"task_claimed": True, "claimed_tasks": tasks, "active_task_id": task_id}


def record_task_selection(variables: dict[str, Any], previous_task_id: object, epoch: str) -> None:
    """Retain selection changes in the same transaction as their current projection.

    Entries remain for the session's lifetime: even a closed task may be the
    predecessor that establishes selection at another open task's window start.
    """
    task_id = variables.get("active_task_id")
    if task_id == previous_task_id and ("task_selection_history" in variables or task_id is None):
        return
    history = variables.get("task_selection_history", [])
    history = list(history) if isinstance(history, list) else []
    history.append({"task_id": task_id, "epoch": epoch})
    variables["task_selection_history"] = history


def task_selection_events(history: object) -> list[tuple[float, str | None]] | None:
    """Decode the canonical history shared by edit and close attribution."""
    if not isinstance(history, list):
        return None
    events: list[tuple[float, str | None]] = []
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("epoch"), str):
            continue
        try:
            at = datetime.fromisoformat(entry["epoch"])
        except ValueError:
            continue
        at = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
        task_id = entry.get("task_id")
        events.append((at.timestamp(), task_id if isinstance(task_id, str) else None))
    return events


def task_selected_at(variables: dict[str, Any], epoch: float) -> str | None:
    """Resolve selection at tool start, preserving delayed and replayed results."""
    selected: str | None = None
    for at, task_id in task_selection_events(variables.get("task_selection_history")) or []:
        if at <= epoch:
            selected = task_id
    return selected


def release_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that releases a claim and keeps the task's edit attribution.

    Escalation and claim handoff pause a task rather than finish it, so the files it
    edited are still its own. Keeping `task_edited_files` preserves what the close
    checklist reads for gates 7, 9, 10 and 12, and what `commit_guard` reads to name
    the owner of a dirty path in a shared worktree.
    """
    tasks = _claimed_tasks(variables)
    tasks.pop(task_id, None)

    # The claims left behind wait on review, landing or close; one receives edits
    # again only through a reclaim (#23665).
    active_task_id = variables.get("active_task_id")
    if active_task_id not in tasks:
        active_task_id = None

    result: dict[str, Any] = {
        "task_claimed": len(tasks) > 0,
        "claimed_tasks": tasks,
        "active_task_id": active_task_id,
    }
    if not tasks:
        result["task_has_commits"] = False
    return result


def remove_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that removes a finished task and its edit attribution."""
    task_files = _task_edited_files(variables)
    task_files.pop(task_id, None)
    task_file_checkouts = _task_edited_file_checkouts(variables)
    task_file_checkouts.pop(task_id, None)

    result = release_claimed_task(variables, task_id)
    result["task_edited_files"] = task_files
    if "task_edited_file_checkouts" in variables:
        result["task_edited_file_checkouts"] = task_file_checkouts
    for key in ("task_edited_file_times", "task_live_edit_starts"):
        if key in variables:
            raw_times = variables.get(key)
            task_file_times = dict(raw_times) if isinstance(raw_times, dict) else {}
            task_file_times.pop(task_id, None)
            result[key] = task_file_times
    return result


def active_task_id_for_edit(variables: dict[str, Any]) -> str | None:
    """Return the task that should receive a new edited-file attribution.

    Only a claim makes a task active, so a claim left behind when the active one
    ended receives no edits until it is reclaimed (#23665).
    """
    active_task_id = variables.get("active_task_id")
    if isinstance(active_task_id, str) and active_task_id in _claimed_tasks(variables):
        return active_task_id
    return None


def claims_without_edit_target(variables: dict[str, Any]) -> bool:
    """Whether the session holds claims but none receives its edits (#23665).

    The active claim ended while the others wait on review, landing or close, so an
    edit must wait for a reclaim that makes one of them active.
    """
    return bool(_claimed_tasks(variables)) and active_task_id_for_edit(variables) is None


def resolve_target_task_id(variables: dict[str, Any], task_ref: Any) -> str | None:
    """Resolve a lifecycle tool task reference to a task UUID tracked in session variables."""
    if task_ref is None:
        return None
    raw_ref = str(task_ref)
    if not raw_ref:
        return None

    claimed = _claimed_tasks(variables)
    task_files = _task_edited_files(variables)
    if raw_ref in claimed or raw_ref in task_files:
        return raw_ref

    aliases = {raw_ref}
    if raw_ref.isdigit():
        aliases.add(f"#{raw_ref}")
    for task_id, display_ref in claimed.items():
        if str(display_ref) in aliases:
            return task_id
    return None


def task_edited_file_set(variables: dict[str, Any], task_id: str | None) -> set[str]:
    """Return repo-relative files attributed to a task."""
    if not task_id:
        return set()
    return set(_task_edited_files(variables).get(task_id, []))


def task_edited_file_set_for_checkout(
    variables: dict[str, Any],
    task_id: str | None,
    checkout_root: str,
) -> set[str]:
    """Return task-attributed paths recorded in one absolute checkout root."""
    root = normalize_task_checkout_root(checkout_root)
    if not task_id or root is None:
        return set()
    return set(_task_edited_file_checkouts(variables).get(task_id, {}).get(root, []))


def task_edited_file_set_with_history(
    variables: dict[str, Any],
    task_id: str | None,
    checkout_root: str,
) -> set[str]:
    """Return live and historical task-attributed paths in one absolute checkout root."""
    root = normalize_task_checkout_root(checkout_root)
    if not task_id or root is None:
        return set()
    checkouts = (
        _task_edited_file_checkouts(variables, name).get(task_id, {})
        for name in ("task_edited_file_checkouts", "task_edited_file_checkouts_history")
    )
    return {path for checkout in checkouts for path in checkout.get(root, [])}


def task_edit_path_covers(parent: str, child: str) -> bool:
    """A root/directory ledger entry covers its descendants, with a path boundary."""
    return parent == "." or parent == child or child.startswith(parent.rstrip("/") + "/")


def overlapping_task_edit_paths(owned: Collection[str], requested: Collection[str]) -> set[str]:
    return {
        path
        for path in owned
        if any(
            task_edit_path_covers(path, other) or task_edit_path_covers(other, path)
            for other in requested
        )
    }


def assert_task_edit_paths_available(
    variables: dict[str, Any],
    task_id: str | None,
    paths: list[str],
    checkout_root: str | None,
    *,
    commit_path_times: dict[str, float] | None = None,
) -> None:
    """Refuse live path overlap between claims instead of guessing edit ownership."""
    requested = set(paths)
    root = normalize_task_checkout_root(checkout_root) or ""
    for other_id, ref in _claimed_tasks(variables).items():
        if other_id == task_id:
            continue
        # Live intervals, not edit history: a reconciled-clean path is free to claim.
        live_by_checkout = variables.get("task_live_edit_starts", {}).get(other_id, {})
        starts = live_by_checkout.get(root, {})
        owned = (
            set(starts)
            if checkout_root is not None
            else {path for live in live_by_checkout.values() for path in live}
        )
        overlap = overlapping_task_edit_paths(owned, requested)
        if commit_path_times is not None:
            overlap = {
                path
                for path in overlap
                if not (
                    isinstance(starts.get(path), (int, float))
                    and path in commit_path_times
                    # Git timestamps cover a whole second; same-second order is unproven.
                    and starts[path] >= commit_path_times[path] + 1
                )
            }
        if overlap:
            raise ValueError(
                f"Paths {', '.join(sorted(overlap))} are live-attributed to {ref}; "
                f"select {ref} with claim_task or finish that task before editing them."
            )


def task_live_checkout_files(variables: dict[str, Any], task_id: str) -> dict[str, set[str]]:
    """Return this task's live attributed paths by checkout root.

    A clean reconcile leaves these pairs in place. A later claim's edit of the
    path in that checkout takes the pair (``transfer_live_checkout_paths``).
    """
    return {
        root: set(files)
        for root, files in _task_edited_file_checkouts(variables).get(task_id, {}).items()
        if files
    }


def record_task_live_edit_starts(
    variables: dict[str, Any], task_id: str, paths: list[str], root: str | None, stamp: float
) -> None:
    """Keep the first edit in each live checkout/path attribution interval."""
    starts = variables.setdefault("task_live_edit_starts", {})
    checkout = starts.setdefault(task_id, {}).setdefault(root or "", {})
    for path in paths:
        prior = checkout.get(path)
        checkout[path] = min(prior, stamp) if isinstance(prior, (int, float)) else stamp


def release_task_live_edit_starts(
    variables: dict[str, Any], task_id: str, paths: list[str], root: str | None
) -> None:
    """A reconciled path's next edit begins a new live attribution interval."""
    starts = variables.get("task_live_edit_starts", {})
    checkouts = starts.get(task_id, {})
    for checkout in list(checkouts):
        if root is not None and checkout != root:
            continue
        for path in paths:
            checkouts[checkout].pop(path, None)
        if not checkouts[checkout]:
            del checkouts[checkout]
    if not checkouts:
        starts.pop(task_id, None)


def transfer_live_checkout_paths(
    variables: dict[str, Any], task_id: str, paths: list[str], root: str
) -> None:
    """Give ``task_id`` the live interval of each path in ``root``.

    One claim holds a path's live interval: the one whose edit recorded it last.
    Earlier claims lose the path from their live checkout ledger and live starts.
    Edit history stays append-only.
    """
    moved = set(paths)
    live = variables.get("task_edited_file_checkouts")
    if isinstance(live, dict):
        ledger = dict(live)
        for other_id, checkouts in live.items():
            if other_id == task_id or not isinstance(checkouts, dict):
                continue
            files = checkouts.get(root)
            if not isinstance(files, list):
                continue
            remaining = [path for path in files if path not in moved]
            if len(remaining) == len(files):
                continue
            kept_checkouts = dict(checkouts)
            if remaining:
                kept_checkouts[root] = remaining
            else:
                del kept_checkouts[root]
            ledger[other_id] = kept_checkouts
        variables["task_edited_file_checkouts"] = ledger
    for other_id in list(variables.get("task_live_edit_starts", {})):
        if other_id != task_id:
            release_task_live_edit_starts(variables, other_id, paths, root)


def task_edited_checkout_paths(
    variables: dict[str, Any], task_id: str
) -> frozenset[tuple[str, str]]:
    """Return this task's exact live and historical checkout/path pairs."""
    return frozenset(
        (root, path)
        for ledger_name in (
            "task_edited_file_checkouts",
            "task_edited_file_checkouts_history",
        )
        for root, files in _task_edited_file_checkouts(variables, ledger_name)
        .get(task_id, {})
        .items()
        for path in files
    )


def task_edited_checkout_history_paths(
    variables: dict[str, Any], task_id: str
) -> frozenset[tuple[str, str]]:
    """Return released task pairs for transcript evidence, not live ownership."""
    return frozenset(
        (root, path)
        for root, files in _task_edited_file_checkouts(
            variables, "task_edited_file_checkouts_history"
        )
        .get(task_id, {})
        .items()
        for path in files
    )


def task_checkout_paths_in_window(
    variables: dict[str, Any],
    task_id: str,
    pairs: frozenset[tuple[str, str]],
    window_start: float,
) -> frozenset[tuple[str, str]]:
    """Keep only pairs with edit-time proof inside the current owner claim."""
    raw = variables.get("task_edited_checkout_times")
    raw_task = raw.get(task_id) if isinstance(raw, dict) else None
    checkout_times = raw_task if isinstance(raw_task, dict) else {}
    path_times = task_edited_file_times(variables, task_id)
    roots_by_path: dict[str, set[str]] = {}
    for root, path in pairs:
        roots_by_path.setdefault(path, set()).add(root)
    kept: set[tuple[str, str]] = set()
    for root, path in pairs:
        raw_root = checkout_times.get(root)
        stamp = raw_root.get(path) if isinstance(raw_root, dict) else None
        if stamp is None and len(roots_by_path[path]) == 1:
            # Existing path timestamps are exact checkout proof only when the
            # path has never been attributed to another checkout for this task.
            stamp = path_times.get(path)
        if (
            not isinstance(stamp, bool)
            and isinstance(stamp, (int, float))
            and isfinite(stamp)
            and stamp >= window_start
        ):
            kept.add((root, path))
    return frozenset(kept)


def other_task_edited_checkout_paths(
    variables: dict[str, Any],
    task_id: str,
    historical_exempt_task_ids: frozenset[str] = frozenset(),
) -> frozenset[tuple[str, str]]:
    """Return other-task pairs, retaining live paths for every other task."""
    return frozenset(
        (root, path)
        for ledger_name in (
            "task_edited_file_checkouts",
            "task_edited_file_checkouts_history",
        )
        for other_task_id, checkouts in _task_edited_file_checkouts(variables, ledger_name).items()
        if other_task_id != task_id
        and not (
            ledger_name == "task_edited_file_checkouts_history"
            and other_task_id in historical_exempt_task_ids
        )
        for root, files in checkouts.items()
        for path in files
    )


def task_edited_file_times(variables: dict[str, Any], task_id: str) -> dict[str, float]:
    """Return epoch seconds of the session's newest recorded edit per path for one task."""
    raw = variables.get("task_edited_file_times") or {}
    raw_task_times = raw.get(task_id) if isinstance(raw, dict) else None
    if not isinstance(raw_task_times, dict):
        return {}
    times: dict[str, float] = {}
    for value, stamp in raw_task_times.items():
        path = normalize_task_edited_path(value)
        if path is None or isinstance(stamp, bool) or not isinstance(stamp, int | float):
            continue
        times[path] = float(stamp)
    return times


def task_checkout_edit_times(
    variables: dict[str, Any], task_id: str, checkout_root: str
) -> dict[str, float]:
    """Return epoch seconds of the newest Gobby edit per path for one task in one checkout."""
    root = normalize_task_checkout_root(checkout_root)
    raw = variables.get("task_edited_checkout_times")
    raw_task = raw.get(task_id) if isinstance(raw, dict) else None
    raw_root = raw_task.get(root) if isinstance(raw_task, dict) and root is not None else None
    if not isinstance(raw_root, dict):
        return {}
    times: dict[str, float] = {}
    for value, stamp in raw_root.items():
        path = normalize_task_edited_path(value)
        if path is None or isinstance(stamp, bool) or not isinstance(stamp, int | float):
            continue
        times[path] = float(stamp)
    return times


def task_checkout_edit_hashes(
    variables: dict[str, Any], task_id: str, checkout_root: str
) -> dict[str, str | None]:
    """Return each path's content hash after one task's Gobby action in one checkout.

    A None hash records a path that was absent at that action.
    """
    root = normalize_task_checkout_root(checkout_root)
    raw = variables.get("task_edited_checkout_hashes")
    raw_task = raw.get(task_id) if isinstance(raw, dict) else None
    raw_root = raw_task.get(root) if isinstance(raw_task, dict) and root is not None else None
    if not isinstance(raw_root, dict):
        return {}
    hashes: dict[str, str | None] = {}
    for value, digest in raw_root.items():
        path = normalize_task_edited_path(value)
        if path is None or not (digest is None or isinstance(digest, str)):
            continue
        hashes[path] = digest
    return hashes


def record_task_content_hashes(
    variables: dict[str, Any], task_id: str, checkout_root: str, digests: dict[str, str | None]
) -> None:
    """Store each path's content hash as one task's newest Gobby action left it."""
    root = normalize_task_checkout_root(checkout_root)
    if root is None or not digests:
        return
    raw = variables.get("task_edited_checkout_hashes")
    all_hashes = dict(raw) if isinstance(raw, dict) else {}
    raw_task = all_hashes.get(task_id)
    task_hashes = dict(raw_task) if isinstance(raw_task, dict) else {}
    raw_root = task_hashes.get(root)
    root_hashes = dict(raw_root) if isinstance(raw_root, dict) else {}
    root_hashes.update(digests)
    task_hashes[root] = root_hashes
    all_hashes[task_id] = task_hashes
    variables["task_edited_checkout_hashes"] = all_hashes


def target_task_has_edits(variables: dict[str, Any], task_id: str | None) -> bool:
    """Return whether the resolved task has attributed edit paths."""
    return bool(task_id and _task_edited_files(variables).get(task_id))


TASK_EDIT_LEDGER_KEYS = (
    "task_edited_files",
    "task_edited_file_times",
    "task_edited_checkout_times",
    "task_edited_checkout_hashes",
    "task_live_edit_starts",
    "task_edited_file_checkouts",
    "task_edited_file_checkouts_history",
    "task_edited_file_checkouts_history_started_at",
)


def attribute_paths_to_task(
    variables: dict[str, Any],
    task_id: str,
    paths: list[str],
    checkout: str | None,
    digests: dict[str, str | None],
    stamp: float,
) -> None:
    """Record ``paths`` as ``task_id``'s edits in ``checkout``, each with the content it left."""
    record_task_live_edit_starts(variables, task_id, paths, checkout, stamp)
    if checkout is not None:
        transfer_live_checkout_paths(variables, task_id, paths, checkout)
    raw_task_files = variables.get("task_edited_files") or {}
    task_files = raw_task_files if isinstance(raw_task_files, dict) else {}
    stored_for_task = task_files.get(task_id, [])
    if not isinstance(stored_for_task, list):
        stored_for_task = [stored_for_task] if stored_for_task else []
    files_for_task = list(dict.fromkeys(str(file) for file in stored_for_task if file))
    files_for_task.extend(path for path in paths if path not in files_for_task)
    task_files = dict(task_files)
    task_files[task_id] = files_for_task
    variables["task_edited_files"] = task_files
    # Epoch seconds of the newest edit per path. The claim window reads it
    # to tell this task's own edits from older dirt. A stale replay never
    # lowers a newer stamp.
    raw_times = variables.get("task_edited_file_times") or {}
    task_times = raw_times if isinstance(raw_times, dict) else {}
    raw_task_times = task_times.get(task_id, {})
    times_for_task = dict(raw_task_times) if isinstance(raw_task_times, dict) else {}
    for path in paths:
        previous = times_for_task.get(path)
        times_for_task[path] = (
            max(float(previous), stamp) if isinstance(previous, (int, float)) else stamp
        )
    task_times = dict(task_times)
    task_times[task_id] = times_for_task
    variables["task_edited_file_times"] = task_times
    if checkout is not None:
        # Path-only times cannot distinguish edits of the same path
        # in different checkouts or different claims.
        raw_pair_times = variables.get("task_edited_checkout_times")
        pair_times = dict(raw_pair_times) if isinstance(raw_pair_times, dict) else {}
        raw_task_pairs = pair_times.get(task_id)
        task_pairs = dict(raw_task_pairs) if isinstance(raw_task_pairs, dict) else {}
        raw_root_times = task_pairs.get(checkout)
        root_times = dict(raw_root_times) if isinstance(raw_root_times, dict) else {}
        for path in paths:
            previous = root_times.get(path)
            root_times[path] = (
                max(float(previous), stamp)
                if isinstance(previous, (int, float)) and not isinstance(previous, bool)
                else stamp
            )
        task_pairs[checkout] = root_times
        pair_times[task_id] = task_pairs
        variables["task_edited_checkout_times"] = pair_times
        record_task_content_hashes(variables, task_id, checkout, digests)
        # Clean paths leave the live starts only. This live ledger keeps
        # the pair until a later claim takes the path with an edit.
        variables.setdefault("task_edited_file_checkouts_history_started_at", time.time())
        for ledger_name in (
            "task_edited_file_checkouts",
            "task_edited_file_checkouts_history",
        ):
            raw_checkouts = variables.get(ledger_name) or {}
            task_checkouts = raw_checkouts if isinstance(raw_checkouts, dict) else {}
            raw_task_checkouts = task_checkouts.get(task_id, {})
            checkouts_for_task = raw_task_checkouts if isinstance(raw_task_checkouts, dict) else {}
            stored_for_checkout = checkouts_for_task.get(checkout, [])
            files_for_checkout = (
                stored_for_checkout if isinstance(stored_for_checkout, list) else []
            )
            files_for_checkout = list(
                dict.fromkeys(str(file) for file in files_for_checkout if file)
            )
            files_for_checkout.extend(path for path in paths if path not in files_for_checkout)
            checkouts_for_task = dict(checkouts_for_task)
            checkouts_for_task[checkout] = files_for_checkout
            task_checkouts = dict(task_checkouts)
            task_checkouts[task_id] = checkouts_for_task
            variables[ledger_name] = task_checkouts


def record_unclaimed_edits(
    variables: dict[str, Any],
    checkout: str,
    paths: list[str],
    digests: dict[str, str | None],
    stamp: float,
) -> None:
    """Hold a write made before any claim, with the content it left, until a claim adopts it."""
    root = normalize_task_checkout_root(checkout)
    if root is None or not paths:
        return
    raw = variables.get("session_unclaimed_edits")
    all_records = dict(raw) if isinstance(raw, dict) else {}
    raw_root = all_records.get(root)
    root_records = dict(raw_root) if isinstance(raw_root, dict) else {}
    for path in paths:
        root_records[path] = {"digest": digests.get(path), "edited_at": stamp}
    all_records[root] = root_records
    variables["session_unclaimed_edits"] = all_records


def adopt_unclaimed_edits(variables: dict[str, Any], task_id: str) -> None:
    """Give ``task_id`` the session's pre-claim writes that still hold the content they left.

    A path that another session or an operator changed since the write, or that another task's
    live attribution holds, stays out. Every pending record is settled here, so the next claim
    starts clean.
    """
    raw = variables.get("session_unclaimed_edits")
    variables["session_unclaimed_edits"] = None
    if not isinstance(raw, dict):
        return
    raw_dirty = variables.get("session_dirty_file_checkouts")
    dirty_checkouts = raw_dirty if isinstance(raw_dirty, dict) else {}
    # Held means another task's live attribution interval, not retained checkout history.
    raw_starts = variables.get("task_live_edit_starts")
    live_starts = raw_starts if isinstance(raw_starts, dict) else {}
    for root, records in raw.items():
        if not isinstance(records, dict):
            continue
        still_dirty = set(dirty_checkouts.get(root) or [])
        held = {
            path
            for other_id, checkouts in live_starts.items()
            if other_id != task_id and isinstance(checkouts, dict)
            for path in checkouts.get(root) or []
        }
        candidates = {
            path: record
            for path, record in records.items()
            if isinstance(record, dict) and path in still_dirty and path not in held
        }
        current = path_fingerprints(Path(root), candidates)
        adopted = {
            path: record
            for path, record in candidates.items()
            if current.get(path) == record.get("digest")
        }
        if not adopted:
            continue
        attribute_paths_to_task(
            variables,
            task_id,
            sorted(adopted),
            root,
            {path: record.get("digest") for path, record in adopted.items()},
            max(float(record.get("edited_at", 0.0)) for record in adopted.values()),
        )
