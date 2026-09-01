import json
import logging
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from gobby.storage.definitions.revisions import (
    get_definitions_revision,
    register_revision_listener,
)
from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionVariableMutation,
)
from gobby.storage.sessions import startup_claim as _startup_claim
from gobby.storage.sessions.startup_claim import StartupClaimState, StartupContextClaim
from gobby.workflows.variable_defaults import (
    load_variable_defaults,
    resolve_session_project_id,
)

__all__ = [
    "SessionVariableManager",
    "StartupClaimState",
    "StartupContextClaim",
]

logger = logging.getLogger(__name__)

_MutationResult = TypeVar("_MutationResult")


def _decode_variables_payload(variables: Any) -> dict[str, Any]:
    if isinstance(variables, dict):
        return variables
    if isinstance(variables, str | bytes | bytearray) and variables:
        try:
            loaded = json.loads(variables)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to decode workflow variables payload: %s", exc)
            return {}
        if isinstance(loaded, dict):
            return loaded
        logger.warning("Ignoring non-object workflow variables payload: %s", type(loaded).__name__)
    return {}


def _sanitize_variables_payload(value: Any) -> Any:
    """Replace PostgreSQL-incompatible NUL characters in JSON-compatible values."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {
            _sanitize_variables_payload(key) if isinstance(key, str) else key: (
                _sanitize_variables_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_variables_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_variables_payload(item) for item in value)
    return value


def _encode_variables_payload(variables: Any) -> str:
    return json.dumps(_sanitize_variables_payload(variables))


def _normalize_string_list(value: Any) -> list[str]:
    """Return the string entries from a stored list variable."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


_LIVE_VARIABLE_MANAGERS: weakref.WeakSet["SessionVariableManager"] = weakref.WeakSet()
_VARIABLE_CACHE_LOCK = threading.Lock()


def _clear_variable_defaults_caches() -> None:
    """Drop every SessionVariableManager defaults cache on a variables revision."""
    with _VARIABLE_CACHE_LOCK:
        for manager in tuple(_LIVE_VARIABLE_MANAGERS):
            manager._defaults_cache.clear()
            manager._defaults_cache_times.clear()


register_revision_listener("variables", _clear_variable_defaults_caches)


class SessionVariableManager:
    """Manages session-scoped shared variables (visible to all workflows).

    Variable resolution layers definition defaults under session overrides,
    ensuring presets are always available even if never explicitly materialized
    into the session row (e.g., ``gobby init`` run mid-session).
    """

    _DEFAULTS_CACHE_TTL = 10.0  # seconds

    def __init__(self, db: HubDatabase):
        self.db = db
        self._defaults_cache: dict[tuple[str | None, int], dict[str, Any]] = {}
        self._defaults_cache_times: dict[tuple[str | None, int], float] = {}
        with _VARIABLE_CACHE_LOCK:
            _LIVE_VARIABLE_MANAGERS.add(self)

    def get_variables(self, session_id: str) -> dict[str, Any]:
        """Get all session variables with definition defaults applied.

        Layers: variable definition defaults < session-stored overrides.
        This ensures presets are always available even if they were never
        explicitly materialized into the session row.
        """
        row = self.db.fetchone(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        )
        session_vars = {}
        if row:
            session_vars = _decode_variables_payload(row["variables"])

        return self._apply_variable_defaults(
            session_vars, resolve_session_project_id(self.db, session_id)
        )

    def _get_variable_defaults(self, project_id: str | None) -> dict[str, Any]:
        """Load enabled defaults for one project, keyed by revision.

        Results are cached for ``_DEFAULTS_CACHE_TTL`` seconds and dropped
        when the variables domain revision advances.
        """
        revision = get_definitions_revision("variables")
        cache_key = (project_id, revision)
        now = time.monotonic()
        cached = self._defaults_cache.get(cache_key)
        cached_at = self._defaults_cache_times.get(cache_key)
        if (
            cached is not None
            and cached_at is not None
            and (now - cached_at) < self._DEFAULTS_CACHE_TTL
        ):
            return deepcopy(cached)

        defaults = load_variable_defaults(self.db, project_id)
        self._defaults_cache[cache_key] = defaults
        self._defaults_cache_times[cache_key] = now
        return deepcopy(defaults)

    def _apply_variable_defaults(
        self, variables: dict[str, Any], project_id: str | None
    ) -> dict[str, Any]:
        """Layer stored variables over project-scoped definition defaults."""
        defaults = self._get_variable_defaults(project_id)
        if not defaults:
            return variables
        return {**defaults, **variables}

    def _mutate_variables(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[_MutationResult, bool]],
        *,
        apply_defaults: bool = False,
    ) -> _MutationResult:
        """Serialize one variable mutation and persist only changed payloads."""
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            variables = _decode_variables_payload(row["variables"]) if row else {}
            if apply_defaults:
                variables = self._apply_variable_defaults(
                    variables, resolve_session_project_id(self.db, session_id)
                )
            result, changed = mutator(variables)
            if not changed:
                return result

            now = datetime.now(UTC).isoformat()
            encoded = _encode_variables_payload(variables)
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (encoded, now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, encoded, now),
                )
            return result

    def set_variable(self, session_id: str, name: str, value: Any) -> None:
        """Set a single session variable (atomic read-modify-write)."""
        self.merge_variables(session_id, {name: value})

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Atomically merge variable updates into session variables.

        A PostgreSQL transaction-scoped advisory lock serializes the read-modify-write,
        preventing concurrent evaluations from clobbering each other.
        Creates the row if it doesn't exist.

        Returns:
            True always (creates row if needed).
        """
        if not updates:
            return True

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            variables.update(updates)
            return True, True

        return self._mutate_variables(session_id, mutate)

    def merge_existing_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Atomically merge updates without creating a missing session row."""
        if not updates:
            return False

        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if row is None:
                return False

            variables = _decode_variables_payload(row["variables"])
            merged = {**variables, **updates}
            if merged == variables:
                return False

            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (_encode_variables_payload(merged), datetime.now(UTC).isoformat(), session_id),
            )
            return True

    def adjust_counter_and_derive_boolean(
        self,
        session_id: str,
        counter_name: str,
        delta: int,
        *,
        boolean_name: str,
    ) -> int:
        """Atomically adjust a non-negative counter and derive its boolean flag."""

        def mutate(variables: dict[str, Any]) -> tuple[int, bool]:
            raw_count = variables.get(counter_name, 0)
            stored_count: int
            if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                stored_count = raw_count
            else:
                stored_count = 0
            count = max(0, stored_count + delta)
            variables[counter_name] = count
            variables[boolean_name] = count > 0
            return count, True

        return self._mutate_variables(session_id, mutate)

    def append_to_bounded_list_variable(
        self,
        session_id: str,
        name: str,
        item: Any,
        *,
        max_items: int,
        updates: dict[str, Any] | None = None,
    ) -> int:
        """Atomically append an item to a bounded list and merge related updates."""
        if max_items < 1:
            raise ValueError("max_items must be positive")

        def mutate(variables: dict[str, Any]) -> tuple[int, bool]:
            stored = variables.get(name, [])
            items = stored if isinstance(stored, list) else []
            bounded_items = [*items, item][-max_items:]
            variables[name] = bounded_items
            if updates:
                variables.update(updates)
            return len(bounded_items), True

        return self._mutate_variables(session_id, mutate)

    def upsert_bounded_list_variable(
        self,
        session_id: str,
        name: str,
        item: Any,
        *,
        identity: Mapping[str, Any],
        max_items: int,
        updates: dict[str, Any] | None = None,
    ) -> int:
        """Atomically replace one identified list item and merge related updates."""
        if not identity:
            raise ValueError("identity must not be empty")
        if max_items < 1:
            raise ValueError("max_items must be positive")

        def mutate(variables: dict[str, Any]) -> tuple[int, bool]:
            stored = variables.get(name, [])
            items = stored if isinstance(stored, list) else []
            retained = [
                existing
                for existing in items
                if not (
                    isinstance(existing, Mapping)
                    and all(existing.get(key) == value for key, value in identity.items())
                )
            ]
            bounded_items = [*retained, item][-max_items:]
            variables[name] = bounded_items
            if updates:
                variables.update(updates)
            return len(bounded_items), True

        return self._mutate_variables(session_id, mutate)

    def upsert_open_tool_error(
        self,
        session_id: str,
        tool: str,
        target_key: str,
        error: str,
        *,
        occurred_at: datetime,
    ) -> None:
        """Atomically insert or increment one canonical unresolved tool error."""
        from gobby.hooks.tool_error_tracker import (
            MAX_TOOL_ERROR_COUNT,
            normalize_open_tool_error_records,
        )

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        timestamp = occurred_at.astimezone(UTC).isoformat(timespec="seconds")
        incoming = normalize_open_tool_error_records(
            [
                {
                    "tool": tool,
                    "target_key": target_key,
                    "error": error,
                    "first_at": timestamp,
                    "last_at": timestamp,
                    "count": 1,
                }
            ]
        )[0]

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            records = normalize_open_tool_error_records(variables.get("open_tool_errors", []))
            match = next(
                (
                    record
                    for record in records
                    if record["tool"] == incoming["tool"]
                    and record["target_key"] == incoming["target_key"]
                ),
                None,
            )
            if match is None:
                records.append(incoming)
            else:
                match["error"] = incoming["error"]
                match["last_at"] = max(match["last_at"], incoming["last_at"])
                match["count"] = min(MAX_TOOL_ERROR_COUNT, match["count"] + 1)
            variables["open_tool_errors"] = normalize_open_tool_error_records(records)
            return None, True

        self._mutate_variables(session_id, mutate)

    def resolve_open_tool_errors(
        self,
        session_id: str,
        tool: str,
        target_key: str,
    ) -> None:
        """Atomically remove the exact canonical tool-and-target error."""
        from gobby.hooks.tool_error_tracker import (
            normalize_open_tool_error_records,
            render_bounded_identity,
            sanitize_record_text,
        )

        canonical_tool = render_bounded_identity(sanitize_record_text(tool))
        canonical_target = render_bounded_identity(sanitize_record_text(target_key))

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            records = normalize_open_tool_error_records(variables.get("open_tool_errors", []))
            retained = [
                record
                for record in records
                if (record["tool"], record["target_key"]) != (canonical_tool, canonical_target)
            ]
            if retained == records:
                return None, False
            variables["open_tool_errors"] = retained
            return None, True

        self._mutate_variables(session_id, mutate)

    def append_to_set_variable(
        self,
        session_id: str,
        name: str,
        values: list[str],
        *,
        preserve_order: bool = False,
    ) -> bool:
        """Atomically append strings to a deduplicated string-list variable.

        A PostgreSQL transaction-scoped advisory lock serializes the read-modify-write,
        preventing concurrent events from clobbering each other. Stored scalars and
        non-string list entries are discarded to preserve the string-list contract.
        Values are sorted by default; ordered mode preserves first-seen order.

        Args:
            session_id: Session ID to scope the variable to.
            name: Variable name (the list to append to).
            values: New values to add (duplicates are ignored).
            preserve_order: Keep first-seen order instead of sorting.

        Returns:
            True always (creates row if needed).
        """
        if not values:
            return True

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            normalized = _normalize_string_list(variables.get(name))
            if preserve_order:
                ordered = list(dict.fromkeys(normalized))
                seen = set(ordered)
                for value in values:
                    if value not in seen:
                        ordered.append(value)
                        seen.add(value)
                variables[name] = ordered
            else:
                existing = set(normalized)
                existing.update(values)
                variables[name] = sorted(existing)
            return True, True

        return self._mutate_variables(session_id, mutate, apply_defaults=True)

    def claim_set_variable_values(
        self,
        session_id: str,
        name: str,
        values: list[str],
    ) -> list[str]:
        """Atomically store and return values that were not already present.

        The returned values preserve input order and contain no duplicates. The
        transaction serializes the read and write so concurrent callers cannot
        both claim the same value.
        """
        if not values:
            return []

        def mutate(variables: dict[str, Any]) -> tuple[list[str], bool]:
            existing = set(_normalize_string_list(variables.get(name)))
            claimed: list[str] = []
            for value in values:
                if value not in existing:
                    existing.add(value)
                    claimed.append(value)

            if not claimed:
                return [], False

            variables[name] = sorted(existing)
            return claimed, True

        return self._mutate_variables(session_id, mutate)

    def append_to_set_variable_and_conditional_merge(
        self,
        session_id: str,
        name: str,
        values: list[str],
        *,
        condition_name: str,
        updates: dict[str, Any],
    ) -> bool:
        """Append set values and conditionally merge updates in one transaction.

        The condition is evaluated against the same row snapshot that receives
        the append, so edit tracking and evidence reset cannot interleave.
        """
        if not values and not updates:
            return True

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            if values:
                existing = set(_normalize_string_list(variables.get(name)))
                existing.update(values)
                variables[name] = sorted(existing)

            if variables.get(condition_name) is True:
                variables.update(updates)

            return True, True

        return self._mutate_variables(session_id, mutate, apply_defaults=True)

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
    ) -> bool:
        """Atomically record one successful mutation observation and its paths."""
        normalized_paths = list(dict.fromkeys(path for path in repo_relative_paths if path))
        if not normalized_paths:
            return False

        from gobby.workflows.task_claim_state import (
            active_task_id_for_edit,
            normalize_task_checkout_root,
        )

        normalized_checkout = normalize_task_checkout_root(checkout_root)

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            stored = variables.get("session_edited_files", [])
            if not isinstance(stored, list):
                stored = [stored] if stored else []
            session_files = list(dict.fromkeys(str(file) for file in stored if file))
            session_files.extend(path for path in normalized_paths if path not in session_files)
            variables["session_edited_files"] = session_files

            task_id = active_task_id_for_edit(variables)
            if task_id:
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
                # uncommitted work from someone else's dirt on a stale attribution.
                raw_times = variables.get("task_edited_file_times") or {}
                task_times = raw_times if isinstance(raw_times, dict) else {}
                raw_task_times = task_times.get(task_id, {})
                times_for_task = dict(raw_task_times) if isinstance(raw_task_times, dict) else {}
                edited_at = time.time()
                for path in normalized_paths:
                    times_for_task[path] = edited_at
                task_times = dict(task_times)
                task_times[task_id] = times_for_task
                variables["task_edited_file_times"] = task_times
                if normalized_checkout is not None:
                    raw_checkouts = variables.get("task_edited_file_checkouts") or {}
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
                    variables["task_edited_file_checkouts"] = task_checkouts
            return True, True

        return self._mutate_variables(session_id, mutate, apply_defaults=True)

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

        return self._mutate_variables(session_id, mutate, apply_defaults=True)

    def claim_startup_context(
        self,
        session_id: str,
        owner_token: str | None = None,
    ) -> StartupContextClaim:
        """Atomically claim the startup context generation on the sessions row."""

        return _startup_claim.claim_startup_context(
            self.db,
            session_id,
            owner_token=owner_token or str(uuid4()),
        )

    def commit_startup_context(
        self,
        session_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        """CAS a matching claimed generation to committed."""

        return _startup_claim.commit_startup_context(self.db, session_id, generation, owner_token)

    def rollback_startup_context(
        self,
        session_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        """CAS a matching claimed generation back to idle."""

        return _startup_claim.rollback_startup_context(self.db, session_id, generation, owner_token)

    def invalidate_startup_context(
        self,
        session_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        """CAS a matching claimed generation to invalidated."""

        return _startup_claim.invalidate_startup_context(
            self.db, session_id, generation, owner_token
        )
