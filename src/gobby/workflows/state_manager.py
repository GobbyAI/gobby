import json
import logging
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
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
from gobby.storage.sessions._contested_expiry import read_session_variables_row
from gobby.storage.sessions.startup_claim import StartupClaimState, StartupContextClaim
from gobby.workflows.session_edit_ledger import SessionEditLedger
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

_STORED_VARIABLES_OBJECT = (
    "CASE WHEN jsonb_typeof(variables) = 'object' THEN variables ELSE '{}'::jsonb END"
)
_SELECT_SCOPED_VARIABLES_TEXT = (
    "SELECT (SELECT COALESCE(jsonb_object_agg(key, stored -> key), '{}'::jsonb) "
    "FROM unnest(%s::text[]) AS requested(key) WHERE stored ? key)::text AS variables "
    f"FROM (SELECT {_STORED_VARIABLES_OBJECT} AS stored FROM session_variables "
    "WHERE session_id = %s) AS session_state"
)
_UPDATE_SCOPED_VARIABLES = (
    "UPDATE session_variables SET variables = "
    f"((%s::jsonb || {_STORED_VARIABLES_OBJECT}) - %s::text[]) || %s::jsonb, "
    "updated_at = %s WHERE session_id = %s"
)


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


# Defaults per hub, shared by every manager on it: callers build a manager per
# call, so a per-instance cache almost never hit (#23359).
_DEFAULTS_CACHE: weakref.WeakKeyDictionary[
    HubDatabase, dict[tuple[str | None, int], tuple[float, dict[str, Any]]]
] = weakref.WeakKeyDictionary()
_VARIABLE_CACHE_LOCK = threading.Lock()


def _clear_variable_defaults_caches() -> None:
    """Drop every hub's cached defaults on a variables revision."""
    with _VARIABLE_CACHE_LOCK:
        _DEFAULTS_CACHE.clear()


register_revision_listener("variables", _clear_variable_defaults_caches)


class SessionVariableManager(SessionEditLedger):
    """Manages session-scoped shared variables (visible to all workflows).

    Variable resolution layers definition defaults under session overrides,
    ensuring presets are always available even if never explicitly materialized
    into the session row (e.g., ``gobby init`` run mid-session).
    """

    _DEFAULTS_CACHE_TTL = 10.0  # seconds

    def __init__(self, db: HubDatabase):
        self.db = db

    def get_variables(self, session_id: str) -> dict[str, Any]:
        """Get all session variables with definition defaults applied.

        Layers: variable definition defaults < session-stored overrides.
        This ensures presets are always available even if they were never
        explicitly materialized into the session row.
        """
        row = read_session_variables_row(self.db, session_id)
        session_vars = _decode_variables_payload(row["variables"]) if row["stored"] else {}
        project_id = row["project_id"]
        return self._apply_variable_defaults(
            session_vars, None if project_id is None else str(project_id)
        )

    def _get_variable_defaults(self, project_id: str | None) -> dict[str, Any]:
        """Load enabled defaults for one project, keyed by revision.

        Results are cached for ``_DEFAULTS_CACHE_TTL`` seconds and dropped
        when the variables domain revision advances.
        """
        revision = get_definitions_revision("variables")
        cache_key = (project_id, revision)
        now = time.monotonic()
        with _VARIABLE_CACHE_LOCK:
            cached = _DEFAULTS_CACHE.get(self.db, {}).get(cache_key)
        if cached is not None and (now - cached[0]) < self._DEFAULTS_CACHE_TTL:
            return deepcopy(cached[1])

        defaults = load_variable_defaults(self.db, project_id)
        with _VARIABLE_CACHE_LOCK:
            _DEFAULTS_CACHE.setdefault(self.db, {})[cache_key] = (now, defaults)
        return deepcopy(defaults)

    def _apply_variable_defaults(
        self, variables: dict[str, Any], project_id: str | None
    ) -> dict[str, Any]:
        """Layer stored variables over project-scoped definition defaults."""
        defaults = self._get_variable_defaults(project_id)
        if not defaults:
            return variables
        return {**defaults, **variables}

    def queue_memory_review(self, session_id: str, candidate: dict[str, str]) -> None:
        """Atomically enqueue a closure on its interactive session."""

        def enqueue(variables: dict[str, Any]) -> tuple[None, bool]:
            stored = (
                []
                if variables.get("_memory_review_stop_delivered")
                else variables.get("_memory_pending_task_reviews") or []
            )
            pending = [dict(item) for item in stored if isinstance(item, Mapping)]
            reviewed = variables.get("_memory_task_review_records") or []
            if not isinstance(reviewed, list):
                reviewed = []
            closure_id = candidate["closure_id"]
            duplicate = any(item.get("closure_id") == closure_id for item in pending) or any(
                isinstance(item, Mapping) and item.get("closure_id") == closure_id
                for item in reviewed
            )
            if not duplicate:
                pending.append(dict(candidate))
            changed = variables.get("_memory_pending_task_reviews") != pending
            variables["_memory_pending_task_reviews"] = pending
            if not duplicate:
                changed = changed or variables.get("_memory_review_stop_delivered") is not False
                variables["_memory_review_stop_delivered"] = False
            return None, changed

        self._mutate_variables(
            session_id,
            enqueue,
            keys=(
                "_memory_review_stop_delivered",
                "_memory_pending_task_reviews",
                "_memory_task_review_records",
            ),
        )

    def _mutate_variables(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[_MutationResult, bool]],
        *,
        keys: Iterable[str],
        apply_defaults: bool = False,
    ) -> _MutationResult:
        """Mutate a declared key scope without decoding or re-encoding unrelated state.

        Callers declare every key their callback reads or writes. The advisory lock
        still covers projection and persistence, including the first insert.
        """
        scope = sorted(set(keys) | {"active_task_id", "task_selection_history"})
        stored_scope: list[str] = _sanitize_variables_payload(scope)
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(_SELECT_SCOPED_VARIABLES_TEXT, (stored_scope, session_id)).fetchone()
            variables = _decode_variables_payload(row["variables"]) if row else {}
            previous_task_id = variables.get("active_task_id")
            previous_history = variables.get("task_selection_history")
            defaults = (
                self._get_variable_defaults(resolve_session_project_id(self.db, session_id))
                if apply_defaults
                else {}
            )
            variables = {
                **{key: defaults[key] for key in stored_scope if key in defaults},
                **variables,
            }
            result, changed = mutator(variables)
            if not changed:
                return result

            now = datetime.now(UTC).isoformat()
            from gobby.workflows.task_claim_state import record_task_selection

            if previous_history is None:
                variables.pop("task_selection_history", None)
            else:
                variables["task_selection_history"] = previous_history
            record_task_selection(variables, previous_task_id, now)
            if undeclared := variables.keys() - (set(scope) | set(stored_scope)):
                raise ValueError(f"Mutation wrote undeclared variable keys: {sorted(undeclared)}")
            encoded = _encode_variables_payload(variables)
            encoded_defaults = _encode_variables_payload(defaults)
            if row:
                conn.execute(
                    _UPDATE_SCOPED_VARIABLES,
                    (encoded_defaults, stored_scope, encoded, now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, (%s::jsonb - %s::text[]) || %s::jsonb, %s)",
                    (session_id, encoded_defaults, stored_scope, encoded, now),
                )
            return result

    def get_variable_subset(self, session_id: str, keys: Iterable[str]) -> dict[str, Any]:
        """Read selected layered values without materializing the session blob."""
        scope = sorted(set(_sanitize_variables_payload(list(keys))))
        row = self.db.fetchone(_SELECT_SCOPED_VARIABLES_TEXT, (scope, session_id))
        stored = _decode_variables_payload(row["variables"]) if row else {}
        defaults = self._get_variable_defaults(resolve_session_project_id(self.db, session_id))
        return {**{key: defaults[key] for key in scope if key in defaults}, **stored}

    def set_variable(self, session_id: str, name: str, value: Any) -> None:
        """Set a single session variable (atomic read-modify-write)."""
        self.merge_variables(session_id, {name: value})

    def merge_variables(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        observed_claim_task_id: str | None = None,
        reconcile_claims: bool = False,
        inherited_active_task_id: str | None = None,
    ) -> bool:
        """Atomically merge variable updates into session variables.

        A PostgreSQL transaction-scoped advisory lock serializes the read-modify-write,
        preventing concurrent evaluations from clobbering each other.
        Creates the row if it doesn't exist.
        Claim observations and reconciliation snapshots additionally retain
        ordered task-row locks while deriving and writing canonical claim state.

        Returns:
            True always (creates row if needed).
        """
        if not updates:
            return True

        if reconcile_claims or observed_claim_task_id is not None:
            from gobby.workflows.task_claim_projection import merge_claimed_task_projection

            return merge_claimed_task_projection(
                self, session_id, updates, observed_claim_task_id, inherited_active_task_id
            )

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            variables.update(updates)
            return True, True

        return self._mutate_variables(session_id, mutate, keys=updates)

    def select_task_claim(self, session_id: str, task_id: str, ref: str) -> bool:
        """Select a canonically owned task while the caller retains its task row lock."""
        from gobby.workflows.task_claim_state import add_claimed_task

        def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
            variables.update(add_claimed_task(variables, task_id, ref))
            return True, True

        return self._mutate_variables(session_id, mutate, keys=("claimed_tasks", "task_claimed"))

    def release_task_claim(self, session_id: str, task_id: str) -> bool:
        """Release one claim under the variable lock, preserving edit attribution."""
        from gobby.workflows.task_claim_state import release_claimed_task

        def release(variables: dict[str, Any]) -> tuple[bool, bool]:
            updates = release_claimed_task(variables, task_id)
            changed = any(variables.get(key) != value for key, value in updates.items())
            variables.update(updates)
            return changed, changed

        return self._mutate_variables(
            session_id, release, keys=("claimed_tasks", "task_claimed", "task_has_commits")
        )

    def merge_existing_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Atomically merge updates without creating a missing session row."""
        if not updates:
            return False

        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            scope = sorted(_sanitize_variables_payload(list(updates)))
            row = conn.execute(_SELECT_SCOPED_VARIABLES_TEXT, (scope, session_id)).fetchone()
            if row is None:
                return False

            variables = _decode_variables_payload(row["variables"])
            merged = {**variables, **updates}
            if merged == variables:
                return False

            conn.execute(
                _UPDATE_SCOPED_VARIABLES,
                (
                    "{}",
                    scope,
                    _encode_variables_payload(merged),
                    datetime.now(UTC).isoformat(),
                    session_id,
                ),
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

        return self._mutate_variables(session_id, mutate, keys=(counter_name, boolean_name))

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

        return self._mutate_variables(session_id, mutate, keys=(name, *(updates or {})))

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

        return self._mutate_variables(session_id, mutate, keys=(name, *(updates or {})))

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

        self._mutate_variables(session_id, mutate, keys=("open_tool_errors",))

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

        self._mutate_variables(session_id, mutate, keys=("open_tool_errors",))

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

        return self._mutate_variables(session_id, mutate, apply_defaults=True, keys=(name,))

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

        return self._mutate_variables(session_id, mutate, keys=(name,))

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

        return self._mutate_variables(
            session_id, mutate, apply_defaults=True, keys=(name, condition_name, *updates)
        )

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
