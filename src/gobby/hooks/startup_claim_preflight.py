"""Pre-submission startup-context claim lifecycle for AGY PreInvocation.

The preflight resolves the canonical session for an AGY turn before the adapter
runs and allocates the startup-context generation on it: a validated
pre-created hint is adopted, otherwise the five-part
``(external_id, machine_id, source, project_id, session_type)`` identity is
resolved and, on a miss, idempotently registered. The claim commits only as a
delivery-receipt effect (``gobby.hooks.receipt_effects``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)

_PREINVOCATION_HOOKS = frozenset({"preinvocation", "pre_invocation"})
_PRIVATE_RESPONSE_KEYS = frozenset(
    {
        "_gobby_startup_claim",
        "_gobby_session_hint_error",
        "_gobby_staged_effects",
        "owner_token",
        "startup_claim_generation",
        "receipt_id",
    }
)
_DEAD_SESSION_STATUSES = frozenset({"deleted", "expired"})

AGY_SESSION_SOURCE = "agy"
AGY_SESSION_TYPE = "terminal"
STARTUP_CLAIM_PREFLIGHT_TIMEOUT_SECONDS = 5.0


class StartupClaimPreflightTimeout(TimeoutError):
    """The bounded preflight did not finish inside the request budget."""


@dataclass(frozen=True)
class StartupClaimLease:
    """Owner token for one preflight claim generation."""

    session_id: str
    generation: int
    owner_token: str


def is_agy_pre_invocation(source: str | None, hook_type: str | None) -> bool:
    """Return whether this envelope is AGY PreInvocation."""
    return (source or "") == AGY_SESSION_SOURCE and (hook_type or "").casefold() in (
        _PREINVOCATION_HOOKS
    )


def strip_private_startup_claim_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Drop claim/receipt fields that must never reach AGY stdout."""
    return {key: value for key, value in response.items() if key not in _PRIVATE_RESPONSE_KEYS}


def preflight_timeout_seconds(adapter_timeout: float) -> float:
    """Bound the preflight by the adapter budget, capped at a small constant."""
    return max(0.0, min(STARTUP_CLAIM_PREFLIGHT_TIMEOUT_SECONDS, float(adapter_timeout)))


def _payload_is_agy_pre_invocation(payload: dict[str, Any]) -> bool:
    source = payload.get("source")
    hook_type = payload.get("hook_type")
    return is_agy_pre_invocation(
        source if isinstance(source, str) else None,
        hook_type if isinstance(hook_type, str) else None,
    )


async def preflight_agy_startup_claim_bounded(
    payload: dict[str, Any],
    hook_manager: Any,
    *,
    timeout_seconds: float,
) -> StartupClaimLease | None:
    """Run the synchronous preflight in a worker thread under a shielded bound.

    On timeout or request cancellation the worker keeps running; whatever
    lease it eventually returns is compare-and-invalidated so no generation
    stays claimed by a request that already exited.
    """
    if not _payload_is_agy_pre_invocation(payload):
        return None
    worker = asyncio.ensure_future(
        asyncio.to_thread(preflight_agy_startup_claim, payload, hook_manager)
    )
    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout=timeout_seconds)
    except TimeoutError as exc:
        worker.add_done_callback(_late_lease_invalidator(hook_manager))
        raise StartupClaimPreflightTimeout(
            f"startup-claim preflight exceeded {timeout_seconds:g}s"
        ) from exc
    except asyncio.CancelledError:
        worker.add_done_callback(_late_lease_invalidator(hook_manager))
        raise


def _late_lease_invalidator(hook_manager: Any) -> Callable[[asyncio.Future[Any]], None]:
    def _invalidate(done: asyncio.Future[Any]) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.debug("Late startup-claim preflight failed: %s", exc)
            return
        lease = done.result()
        if not isinstance(lease, StartupClaimLease):
            return
        logger.warning(
            "Invalidating startup claim generation %s for session %s: preflight finished "
            "after its request exited",
            lease.generation,
            lease.session_id,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            invalidate_agy_startup_claim(hook_manager, lease)
            return
        loop.run_in_executor(None, invalidate_agy_startup_claim, hook_manager, lease)

    return _invalidate


def preflight_agy_startup_claim(
    payload: dict[str, Any],
    hook_manager: Any,
) -> StartupClaimLease | None:
    """Resolve, adopt, or register the canonical session and claim startup context."""
    if not _payload_is_agy_pre_invocation(payload):
        return None

    session_manager = _session_manager(hook_manager)
    db = getattr(session_manager, "db", None)
    if session_manager is None or db is None:
        return None

    conversation_id = _conversation_id(payload)
    session_id, rejected_hint = _adopt_hinted_session(session_manager, payload, conversation_id)
    if session_id is None:
        session_id = _resolve_or_register_session(
            session_manager,
            hook_manager,
            payload,
            conversation_id,
            rejected_hint=rejected_hint,
        )
    if session_id is None:
        return None

    owner_token = _undelivered_owner_token(db, session_id) or str(uuid4())
    claim = SessionVariableManager(db).claim_startup_context(session_id, owner_token=owner_token)
    if claim.mode != "full" or claim.state != "claimed" or claim.owner_token != owner_token:
        return None

    payload["_gobby_startup_claim"] = {
        "session_id": session_id,
        "generation": claim.generation,
        "owner_token": owner_token,
    }
    return StartupClaimLease(session_id, claim.generation, owner_token)


def rollback_agy_startup_claim(hook_manager: Any, lease: StartupClaimLease) -> None:
    """CAS the preflight claim back to idle after a failed adapter attempt."""
    _mutate_claim(hook_manager, lease, "rollback")


def invalidate_agy_startup_claim(hook_manager: Any, lease: StartupClaimLease) -> None:
    """CAS the preflight claim to invalidated after adapter timeout."""
    _mutate_claim(hook_manager, lease, "invalidate")


def _mutate_claim(hook_manager: Any, lease: StartupClaimLease, action: str) -> None:
    db = getattr(_session_manager(hook_manager), "db", None)
    if db is None:
        return
    manager = SessionVariableManager(db)
    if action == "invalidate":
        manager.invalidate_startup_context(lease.session_id, lease.generation, lease.owner_token)
        return
    manager.rollback_startup_context(lease.session_id, lease.generation, lease.owner_token)


def _session_manager(hook_manager: Any) -> Any | None:
    return getattr(hook_manager, "session_manager", None) or getattr(
        hook_manager, "_session_manager", None
    )


def _adopt_hinted_session(
    session_manager: Any,
    payload: dict[str, Any],
    conversation_id: str | None,
) -> tuple[str | None, str | None]:
    """Adopt the pre-created hint only after its full identity validates.

    Returns ``(adopted_session_id, rejected_session_id)``; a rejected hint is
    reported so ordinary resolution never recovers or moves that row.
    """
    hint = payload.get("_platform_session_id")
    if not isinstance(hint, str) or not hint.strip():
        return None, None
    hint = hint.strip()

    row = _lookup_session(session_manager, hint)
    if row is None:
        return None, None

    mismatch = _hint_mismatch(row, payload, conversation_id)
    if mismatch is not None:
        payload["_gobby_session_hint_error"] = mismatch
        logger.warning("AGY startup-claim preflight: %s", mismatch)
        return None, _row_id(row) or hint

    session_id = _row_id(row) or hint
    _bind_conversation(session_manager, row, session_id, conversation_id)
    return session_id, None


def _bind_conversation(
    session_manager: Any,
    row: Any,
    session_id: str,
    conversation_id: str | None,
) -> None:
    """Bind conversationId onto an adopted placeholder row (session_type preserved)."""
    if not conversation_id:
        return
    current = getattr(row, "external_id", None)
    if current == conversation_id:
        return
    update = getattr(session_manager, "update", None)
    if not callable(update):
        return
    try:
        update(session_id, external_id=conversation_id)
    except Exception as exc:
        logger.warning(
            "Failed to bind AGY conversation %s onto adopted session %s: %s",
            conversation_id,
            session_id,
            exc,
        )


def _resolve_or_register_session(
    session_manager: Any,
    hook_manager: Any,
    payload: dict[str, Any],
    conversation_id: str | None,
    *,
    rejected_hint: str | None = None,
) -> str | None:
    """Resolve the five-part identity, registering the minimal row on a miss."""
    if not conversation_id:
        return None
    try:
        machine_id = require_local_machine_id(
            _payload_field(payload, "machine_id"),
            resource_kind="session",
            resource_id=conversation_id,
        )
    except MachineOwnershipMismatchError as exc:
        logger.warning("AGY startup-claim preflight rejected foreign machine: %s", exc)
        return None
    except Exception as exc:
        logger.warning("AGY startup-claim preflight could not resolve machine identity: %s", exc)
        return None

    project_id = _resolve_project_id(hook_manager, payload)
    if project_id is None:
        return None

    finder = getattr(session_manager, "find_by_external_id", None)
    if callable(finder):
        try:
            existing = finder(
                external_id=conversation_id,
                project_id=project_id,
                source=AGY_SESSION_SOURCE,
                session_type=AGY_SESSION_TYPE,
            )
        except Exception as exc:
            logger.warning("AGY startup-claim preflight lookup failed: %s", exc)
            return None
        session_id = _row_id(existing)
        if session_id is not None:
            return session_id

    if rejected_hint is not None and _identity_owned_by(
        session_manager, conversation_id, rejected_hint
    ):
        logger.warning(
            "AGY startup-claim preflight: conversation %s is owned by rejected hint %s; "
            "not registering or recovering it this turn",
            conversation_id,
            rejected_hint,
        )
        return None

    register = getattr(session_manager, "register", None)
    if not callable(register):
        return None
    try:
        created = register(
            external_id=conversation_id,
            machine_id=machine_id,
            source=AGY_SESSION_SOURCE,
            project_id=project_id,
            session_type=AGY_SESSION_TYPE,
            workspace_path=_envelope_workspace(payload),
        )
    except Exception as exc:
        logger.warning(
            "AGY startup-claim preflight could not register session for conversation %s: %s",
            conversation_id,
            exc,
        )
        return None
    return _row_id(created)


def _identity_owned_by(session_manager: Any, conversation_id: str, session_id: str) -> bool:
    """Whether registration would recover (and move) the rejected hinted row."""
    finder = getattr(session_manager, "find_by_external_id_any_project", None)
    if not callable(finder):
        return False
    try:
        owner = finder(
            external_id=conversation_id,
            source=AGY_SESSION_SOURCE,
            session_type=AGY_SESSION_TYPE,
        )
    except Exception as exc:
        logger.debug("Any-project identity lookup failed for %s: %s", conversation_id, exc)
        return False
    return _row_id(owner) == session_id


def _resolve_project_id(hook_manager: Any, payload: dict[str, Any]) -> str | None:
    explicit = _payload_field(payload, "project_id")
    if explicit:
        return explicit
    workspace = _envelope_workspace(payload)
    if not workspace:
        return None
    resolver = getattr(hook_manager, "_resolve_project_id", None)
    if callable(resolver):
        try:
            resolved = resolver(None, workspace)
        except Exception as exc:
            logger.debug("Hook manager could not resolve project for %s: %s", workspace, exc)
            resolved = None
        if isinstance(resolved, str) and resolved:
            return resolved
    from gobby.utils.project_context import get_project_context

    try:
        context = get_project_context(Path(workspace))
    except Exception as exc:
        logger.debug("Project context walk-up failed for %s: %s", workspace, exc)
        return None
    project_id = context.get("id") if isinstance(context, dict) else None
    return str(project_id) if project_id else None


def _undelivered_owner_token(db: Any, session_id: str) -> str | None:
    """Adopt the owner of a claim whose delivery was released so it re-presents."""
    from gobby.storage.hook_receipts import find_undelivered_startup_context

    try:
        staged = find_undelivered_startup_context(db, session_id=session_id)
    except Exception as exc:
        logger.debug("Undelivered startup-context lookup failed for %s: %s", session_id, exc)
        return None
    if not staged or staged.get("session_id") not in {None, session_id}:
        return None
    token = staged.get("owner_token")
    return token if isinstance(token, str) and token else None


def _lookup_session(session_manager: Any, hint: str) -> Any | None:
    getter = getattr(session_manager, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(hint)
    except Exception:
        return None


def _row_id(row: Any) -> str | None:
    session_id = getattr(row, "id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _hint_mismatch(
    row: Any,
    payload: dict[str, Any],
    conversation_id: str | None,
) -> str | None:
    """Return a diagnostic when the hinted row's identity disagrees with the envelope.

    Every field the envelope supplies must match exactly; a NULL row column is
    a mismatch, not a wildcard.
    """
    problems: list[str] = []
    source = payload.get("source")
    row_source = getattr(row, "source", None)
    if isinstance(source, str) and row_source != source:
        problems.append(f"source={row_source} (expected {source})")

    for field in ("project_id", "machine_id", "session_type"):
        expected = _payload_field(payload, field)
        actual = getattr(row, field, None)
        if expected and actual != expected:
            problems.append(f"{field}={actual} (expected {expected})")

    envelope_workspace = _envelope_workspace(payload)
    row_workspace = getattr(row, "workspace_path", None)
    if envelope_workspace and row_workspace != envelope_workspace:
        problems.append(f"workspace_path={row_workspace} (expected {envelope_workspace})")

    expected_generation = _payload_int(payload, "workspace_generation")
    row_generation = getattr(row, "workspace_generation", None)
    if expected_generation is not None and row_generation != expected_generation:
        problems.append(
            f"workspace_generation={row_generation} (expected {expected_generation}; "
            "concurrent workspace switch)"
        )

    if getattr(row, "tombstoned", False) is True:
        problems.append("workspace is tombstoned")

    status = getattr(row, "status", None)
    if isinstance(status, str) and status in _DEAD_SESSION_STATUSES:
        problems.append(f"status={status} (session is no longer live)")

    bound = getattr(row, "external_id", None)
    if (
        conversation_id
        and isinstance(bound, str)
        and bound
        and bound != conversation_id
        and _has_pending_transcript(row)
    ):
        problems.append(
            f"external_id={bound} (expected {conversation_id}; "
            "row carries a pending transcript for another conversation)"
        )

    if not problems:
        return None
    session_id = getattr(row, "id", None) or _payload_field(payload, "_platform_session_id")
    return f"pre-created session hint {session_id} rejected: {', '.join(problems)}"


def _has_pending_transcript(row: Any) -> bool:
    transcript_path = getattr(row, "transcript_path", None)
    if isinstance(transcript_path, str) and transcript_path.strip():
        return True
    for field in ("message_count", "turn_count"):
        value = getattr(row, field, 0)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _conversation_id(payload: dict[str, Any]) -> str | None:
    input_data = payload.get("input_data")
    if not isinstance(input_data, dict):
        return None
    for key in ("conversationId", "conversation_id", "session_id"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_field(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    input_data = payload.get("input_data")
    if isinstance(input_data, dict):
        nested = input_data.get(name)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _payload_int(payload: dict[str, Any], name: str) -> int | None:
    for container in (payload, payload.get("input_data")):
        if not isinstance(container, dict):
            continue
        value = container.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _envelope_workspace(payload: dict[str, Any]) -> str | None:
    input_data = payload.get("input_data")
    if not isinstance(input_data, dict):
        return None
    cwd = input_data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    paths = input_data.get("workspace_paths")
    if not isinstance(paths, list):
        paths = input_data.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0].strip():
        return paths[0].strip()
    return None
