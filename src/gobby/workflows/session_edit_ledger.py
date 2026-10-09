"""Checkout-scoped session and task edit-attribution ledgers."""

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
_MutationResult = TypeVar("_MutationResult")


def _session_dirty_file_checkouts(variables: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return a mutable copy of ``session_dirty_file_checkouts`` (checkout root -> paths)."""
    raw = variables.get("session_dirty_file_checkouts")
    if not isinstance(raw, dict):
        return {}
    return {
        str(root): list(dict.fromkeys(str(path) for path in paths if path))
        for root, paths in raw.items()
        if isinstance(paths, list)
    }


class SessionEditLedger(ABC):
    """Edit attribution policies use the manager's serialized variable mutation."""

    @abstractmethod
    def _mutate_variables(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[_MutationResult, bool]],
        *,
        keys: Iterable[str],
        apply_defaults: bool = False,
    ) -> _MutationResult:
        raise NotImplementedError

    def record_edited_file(
        self,
        session_id: str,
        repo_relative_path: str,
        checkout_root: str | None = None,
    ) -> bool:
        """Record a successful repo file edit in session and active-task ledgers."""
        return self.record_edited_files(
            session_id,
            [repo_relative_path],
            checkout_root=checkout_root,
        )

    def record_edited_files(
        self,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
        edited_at: float | None = None,
        started_at: float | None = None,
        attribute_to_task: bool = True,
    ) -> bool:
        """Atomically record one successful mutation observation and its paths.

        ``edited_at`` is the epoch time the edit hook fired; a replayed envelope
        carries its original time, so the ledger never mistakes replay time for
        edit time.
        """
        normalized_paths = list(dict.fromkeys(path for path in repo_relative_paths if path))
        if not normalized_paths:
            return False
        stamp = time.time() if edited_at is None else edited_at

        from gobby.workflows.task_claim_state import (
            active_task_id_for_edit,
            assert_task_edit_paths_available,
            normalize_task_checkout_root,
            record_task_live_edit_starts,
            task_selected_at,
        )

        normalized_checkout = normalize_task_checkout_root(checkout_root)

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            task_id = (
                active_task_id_for_edit(variables)
                if started_at is None
                else task_selected_at(variables, started_at)
            )
            if not attribute_to_task:
                task_id = None
            if started_at is not None and (
                task_id is None or task_id not in variables.get("claimed_tasks", {})
            ):
                logger.warning(
                    "Edit attribution has no owned selection at tool start; use claim_task(task_id) "
                    "to select the task before retrying the edit (session %s)",
                    session_id,
                )
                task_id = None
            if task_id is not None:
                assert_task_edit_paths_available(
                    variables, task_id, normalized_paths, normalized_checkout
                )
            stored = variables.get("session_edited_files", [])
            if not isinstance(stored, list):
                stored = [stored] if stored else []
            session_files = list(dict.fromkeys(str(file) for file in stored if file))
            session_files.extend(path for path in normalized_paths if path not in session_files)
            variables["session_edited_files"] = session_files
            # Paths edited since the last reconcile released them as clean; this
            # is what ``has_dirty_files`` reads, so no hook has to ask git.
            stored_dirty = variables.get("session_dirty_files", [])
            if not isinstance(stored_dirty, list):
                stored_dirty = [stored_dirty] if stored_dirty else []
            dirty_files = list(dict.fromkeys(str(file) for file in stored_dirty if file))
            dirty_files.extend(path for path in normalized_paths if path not in dirty_files)
            variables["session_dirty_files"] = dirty_files
            if normalized_checkout is not None:
                # Which checkout each dirty path was edited in: a reconcile run
                # from another worktree's git cannot see this dirt and must not
                # release it.
                dirty_checkouts = _session_dirty_file_checkouts(variables)
                files_for_dirty_checkout = dirty_checkouts.get(normalized_checkout, [])
                files_for_dirty_checkout.extend(
                    path for path in normalized_paths if path not in files_for_dirty_checkout
                )
                dirty_checkouts[normalized_checkout] = files_for_dirty_checkout
                variables["session_dirty_file_checkouts"] = dirty_checkouts

            if task_id:
                record_task_live_edit_starts(
                    variables, task_id, normalized_paths, normalized_checkout, stamp
                )
                raw_task_files = variables.get("task_edited_files") or {}
                task_files = raw_task_files if isinstance(raw_task_files, dict) else {}
                stored_for_task = task_files.get(task_id, [])
                if not isinstance(stored_for_task, list):
                    stored_for_task = [stored_for_task] if stored_for_task else []
                files_for_task = list(dict.fromkeys(str(file) for file in stored_for_task if file))
                files_for_task.extend(
                    path for path in normalized_paths if path not in files_for_task
                )
                task_files = dict(task_files)
                task_files[task_id] = files_for_task
                variables["task_edited_files"] = task_files
                # Epoch seconds of the newest edit per path: release_task_paths compares
                # it against the last commit touching the path to tell this task's own
                # uncommitted work from someone else's dirt on a stale attribution. A
                # stale replay never lowers a newer stamp.
                raw_times = variables.get("task_edited_file_times") or {}
                task_times = raw_times if isinstance(raw_times, dict) else {}
                raw_task_times = task_times.get(task_id, {})
                times_for_task = dict(raw_task_times) if isinstance(raw_task_times, dict) else {}
                for path in normalized_paths:
                    previous = times_for_task.get(path)
                    times_for_task[path] = (
                        max(float(previous), stamp) if isinstance(previous, (int, float)) else stamp
                    )
                task_times = dict(task_times)
                task_times[task_id] = times_for_task
                variables["task_edited_file_times"] = task_times
                if normalized_checkout is not None:
                    # Clean paths leave the live ledger, but close evidence still
                    # needs their task attribution for later transcript edits.
                    variables.setdefault(
                        "task_edited_file_checkouts_history_started_at", time.time()
                    )
                    for ledger_name in (
                        "task_edited_file_checkouts",
                        "task_edited_file_checkouts_history",
                    ):
                        raw_checkouts = variables.get(ledger_name) or {}
                        task_checkouts = raw_checkouts if isinstance(raw_checkouts, dict) else {}
                        raw_task_checkouts = task_checkouts.get(task_id, {})
                        checkouts_for_task = (
                            raw_task_checkouts if isinstance(raw_task_checkouts, dict) else {}
                        )
                        stored_for_checkout = checkouts_for_task.get(normalized_checkout, [])
                        files_for_checkout = (
                            stored_for_checkout if isinstance(stored_for_checkout, list) else []
                        )
                        files_for_checkout = list(
                            dict.fromkeys(str(file) for file in files_for_checkout if file)
                        )
                        files_for_checkout.extend(
                            path for path in normalized_paths if path not in files_for_checkout
                        )
                        checkouts_for_task = dict(checkouts_for_task)
                        checkouts_for_task[normalized_checkout] = files_for_checkout
                        task_checkouts = dict(task_checkouts)
                        task_checkouts[task_id] = checkouts_for_task
                        variables[ledger_name] = task_checkouts
            return True, True

        return self._mutate_variables(
            session_id,
            mutate,
            apply_defaults=True,
            keys=(
                "claimed_tasks",
                "session_edited_files",
                "session_dirty_files",
                "session_dirty_file_checkouts",
                "task_edited_files",
                "task_edited_file_times",
                "task_live_edit_starts",
                "task_edited_file_checkouts",
                "task_edited_file_checkouts_history",
                "task_edited_file_checkouts_history_started_at",
            ),
        )

    def release_session_dirty_files(
        self,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
    ) -> list[str]:
        """Atomically drop paths git reconciled as clean from the session dirty ledger.

        ``checkout_root`` is the checkout whose git reported the paths clean. A
        path stays dirty while any other checkout still records an edit of it,
        and a path recorded only in other checkouts is never released here.
        """
        from gobby.workflows.task_claim_state import (
            normalize_task_checkout_root,
            normalize_task_edited_path,
        )

        requested = {
            path
            for value in repo_relative_paths
            if (path := normalize_task_edited_path(value)) is not None
        }
        root = normalize_task_checkout_root(checkout_root)

        def mutate(variables: dict[str, Any]) -> tuple[list[str], bool]:
            stored = variables.get("session_dirty_files", [])
            values = stored if isinstance(stored, list) else []
            dirty_checkouts = _session_dirty_file_checkouts(variables)
            checkouts_changed = False
            if root is not None and root in dirty_checkouts:
                kept = [path for path in dirty_checkouts[root] if path not in requested]
                checkouts_changed = len(kept) != len(dirty_checkouts[root])
                if kept:
                    dirty_checkouts[root] = kept
                else:
                    del dirty_checkouts[root]
            still_dirty_elsewhere = {path for paths in dirty_checkouts.values() for path in paths}
            released: list[str] = []
            remaining: list[str] = []
            for value in values:
                normalized = normalize_task_edited_path(value)
                if normalized is None:
                    continue
                releasable = normalized in requested and normalized not in still_dirty_elsewhere
                bucket = released if releasable else remaining
                if normalized not in bucket:
                    bucket.append(normalized)
            if not released and not checkouts_changed:
                return released, False
            variables["session_dirty_files"] = remaining
            variables["session_dirty_file_checkouts"] = dirty_checkouts
            return released, True

        return self._mutate_variables(
            session_id,
            mutate,
            apply_defaults=True,
            keys=("session_dirty_files", "session_dirty_file_checkouts"),
        )

    def release_task_edited_files(
        self,
        session_id: str,
        task_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Atomically release owner-confirmed paths from one task attribution ledger."""
        from gobby.workflows.task_claim_state import (
            normalize_task_checkout_root,
            normalize_task_edited_path,
            release_task_live_edit_starts,
        )

        requested = list(
            dict.fromkeys(
                path
                for value in repo_relative_paths
                if (path := normalize_task_edited_path(value)) is not None
            )
        )
        requested_set = set(requested)
        normalized_checkout = normalize_task_checkout_root(checkout_root)

        def mutate(variables: dict[str, Any]) -> tuple[tuple[list[str], list[str]], bool]:
            raw_task_files = variables.get("task_edited_files") or {}
            task_files = raw_task_files if isinstance(raw_task_files, dict) else {}
            stored = task_files.get(task_id, [])
            files_for_task = stored if isinstance(stored, list) else []

            raw_checkouts = variables.get("task_edited_file_checkouts") or {}
            task_checkouts = raw_checkouts if isinstance(raw_checkouts, dict) else {}
            raw_task_checkouts = task_checkouts.get(task_id, {})
            checkouts_for_task = raw_task_checkouts if isinstance(raw_task_checkouts, dict) else {}
            scoped_paths = (
                checkouts_for_task.get(normalized_checkout, [])
                if normalized_checkout is not None
                else None
            )
            has_scoped_attribution = (
                normalized_checkout is not None
                and normalized_checkout in checkouts_for_task
                and isinstance(scoped_paths, list)
            )
            scoped_requested = set(scoped_paths or []) & requested_set
            retained_scoped_paths = {
                str(path)
                for root, paths in checkouts_for_task.items()
                if root != normalized_checkout and isinstance(paths, list)
                for path in paths
            }

            released: list[str] = []
            remaining: list[str] = []
            for value in files_for_task:
                normalized = normalize_task_edited_path(value)
                should_release = normalized in requested_set
                if has_scoped_attribution:
                    should_release = (
                        normalized in scoped_requested and normalized not in retained_scoped_paths
                    )
                if should_release:
                    if normalized is not None and normalized not in released:
                        released.append(normalized)
                    continue
                if normalized is not None and normalized not in remaining:
                    remaining.append(normalized)

            if has_scoped_attribution:
                released = [path for path in requested if path in scoped_requested]
            if not released:
                return (released, remaining), False

            updated_task_files = dict(task_files)
            if remaining:
                updated_task_files[task_id] = remaining
            else:
                updated_task_files.pop(task_id, None)
            variables["task_edited_files"] = updated_task_files
            release_task_live_edit_starts(variables, task_id, released, normalized_checkout)
            raw_times = variables.get("task_edited_file_times") or {}
            task_times = raw_times if isinstance(raw_times, dict) else {}
            if task_id in task_times:
                raw_task_times = task_times.get(task_id)
                stored_times = raw_task_times if isinstance(raw_task_times, dict) else {}
                remaining_times = {
                    path: stamp
                    for path, stamp in stored_times.items()
                    if normalize_task_edited_path(path) not in released
                }
                updated_task_times = dict(task_times)
                if remaining_times:
                    updated_task_times[task_id] = remaining_times
                else:
                    updated_task_times.pop(task_id, None)
                variables["task_edited_file_times"] = updated_task_times
            if has_scoped_attribution and normalized_checkout is not None:
                updated_checkouts_for_task = dict(checkouts_for_task)
                remaining_checkout_paths = [
                    path for path in (scoped_paths or []) if path not in requested_set
                ]
                if remaining_checkout_paths:
                    updated_checkouts_for_task[normalized_checkout] = remaining_checkout_paths
                else:
                    updated_checkouts_for_task.pop(normalized_checkout, None)
                updated_task_checkouts = dict(task_checkouts)
                if updated_checkouts_for_task:
                    updated_task_checkouts[task_id] = updated_checkouts_for_task
                else:
                    updated_task_checkouts.pop(task_id, None)
                variables["task_edited_file_checkouts"] = updated_task_checkouts
            return (released, remaining), True

        return self._mutate_variables(
            session_id,
            mutate,
            apply_defaults=True,
            keys=(
                "task_edited_files",
                "task_edited_file_checkouts",
                "task_edited_file_times",
                "task_live_edit_starts",
            ),
        )
