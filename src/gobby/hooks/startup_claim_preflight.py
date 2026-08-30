"""Pre-submission startup-context claim lifecycle for AGY PreInvocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from gobby.workflows.state_manager import SessionVariableManager

_PREINVOCATION_HOOKS = frozenset({"preinvocation", "pre_invocation"})
_PRIVATE_RESPONSE_KEYS = frozenset(
    {
        "_gobby_startup_claim",
        "_gobby_session_hint_error",
        "owner_token",
        "startup_claim_generation",
        "receipt_id",
    }
)


@dataclass(frozen=True)
class StartupClaimLease:
    """Owner token for one preflight claim generation."""

    session_id: str
    generation: int
    owner_token: str


def is_agy_pre_invocation(source: str | None, hook_type: str | None) -> bool:
    """Return whether this envelope is AGY PreInvocation."""
    return (source or "") == "agy" and (hook_type or "").casefold() in _PREINVOCATION_HOOKS


def strip_private_startup_claim_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Drop claim/receipt fields that must never reach AGY stdout."""
    return {key: value for key, value in response.items() if key not in _PRIVATE_RESPONSE_KEYS}


def preflight_agy_startup_claim(
    payload: dict[str, Any],
    hook_manager: Any,
) -> StartupClaimLease | None:
    """Validate a pre-created hint and claim startup context before adapter execution."""
    source = payload.get("source")
    hook_type = payload.get("hook_type")
    if not is_agy_pre_invocation(
        source if isinstance(source, str) else None,
        hook_type if isinstance(hook_type, str) else None,
    ):
        return None

    session_manager = getattr(hook_manager, "session_manager", None) or getattr(
        hook_manager, "_session_manager", None
    )
    db = getattr(session_manager, "db", None)
    if session_manager is None or db is None:
        return None

    hint = payload.get("_platform_session_id")
    if not isinstance(hint, str) or not hint.strip():
        return None
    hint = hint.strip()

    row = _lookup_session(session_manager, hint)
    if row is None:
        return None

    mismatch = _hint_mismatch(row, payload)
    if mismatch is not None:
        payload["_gobby_session_hint_error"] = mismatch
        return None

    session_id = str(getattr(row, "id", hint))
    owner_token = str(uuid4())
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
    session_manager = getattr(hook_manager, "session_manager", None) or getattr(
        hook_manager, "_session_manager", None
    )
    db = getattr(session_manager, "db", None)
    if db is None:
        return
    manager = SessionVariableManager(db)
    if action == "invalidate":
        manager.invalidate_startup_context(lease.session_id, lease.generation, lease.owner_token)
        return
    manager.rollback_startup_context(lease.session_id, lease.generation, lease.owner_token)


def _lookup_session(session_manager: Any, hint: str) -> Any | None:
    getter = getattr(session_manager, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(hint)
    except Exception:
        return None


def _hint_mismatch(row: Any, payload: dict[str, Any]) -> str | None:
    problems: list[str] = []
    source = payload.get("source")
    row_source = getattr(row, "source", None)
    if isinstance(source, str) and row_source not in {None, source}:
        problems.append(f"source={row_source} (expected {source})")

    envelope_project = _payload_field(payload, "project_id")
    row_project = getattr(row, "project_id", None)
    if envelope_project and row_project not in {None, envelope_project}:
        problems.append(f"project_id={row_project} (expected {envelope_project})")

    envelope_machine = _payload_field(payload, "machine_id")
    row_machine = getattr(row, "machine_id", None)
    if envelope_machine and row_machine not in {None, envelope_machine}:
        problems.append(f"machine_id={row_machine} (expected {envelope_machine})")

    envelope_type = _payload_field(payload, "session_type")
    row_type = getattr(row, "session_type", None)
    if envelope_type and row_type not in {None, envelope_type}:
        problems.append(f"session_type={row_type} (expected {envelope_type})")

    envelope_workspace = _envelope_workspace(payload)
    row_workspace = getattr(row, "workspace_path", None)
    if envelope_workspace and row_workspace not in {None, envelope_workspace}:
        problems.append(f"workspace_path={row_workspace} (expected {envelope_workspace})")

    if getattr(row, "tombstoned", False) is True:
        problems.append("workspace is tombstoned")

    if not problems:
        return None
    session_id = getattr(row, "id", None) or _payload_field(payload, "_platform_session_id")
    return f"pre-created session hint {session_id} rejected: {', '.join(problems)}"


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
