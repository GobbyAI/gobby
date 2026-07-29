"""Identity fence for hooks emitted by managed agent sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from gobby.agents.resume_finalization import finalize_resume_handoff_threadsafe
from gobby.hooks.events import HookEvent, HookEventType
from gobby.storage.hub.protocol import HubDatabase

_PROVISIONAL_RESUME_PHASES = {"launch_requested", "runtime_persisted"}

# The identity fence guards terminal side effects only: a stale SessionEnd or
# Stop from a dead process must not terminalize the durable session's current
# run. Non-terminal hooks are never gated, so a transient identity gap cannot
# stall a live CLI mid-conversation.
TERMINAL_INGRESS_HOOK_TYPES = frozenset(
    {
        HookEventType.SESSION_END,
        HookEventType.STOP,
        HookEventType.AFTER_AGENT,
    }
)


class _SessionManager(Protocol):
    def get(self, session_id: str) -> Any | None: ...


class _AgentRunManager(Protocol):
    def get(self, run_id: str) -> Any | None: ...


@dataclass(frozen=True)
class ManagedHookIngress:
    """Result of validating a hook against its durable agent-run owner."""

    accepted: bool
    managed: bool
    run_id: str | None = None
    ambiguous: bool = False
    reason: str | None = None


class AgentRunIngressRetryableError(RuntimeError):
    """A managed hook cannot yet be assigned to an exact durable run."""

    def __init__(self, *, session_id: str, expected_run_id: str, reason: str) -> None:
        super().__init__(reason)
        self.session_id = session_id
        self.expected_run_id = expected_run_id
        self.reason = reason


def validate_managed_agent_hook(
    event: HookEvent,
    *,
    session_manager: _SessionManager,
    agent_run_manager: _AgentRunManager,
    database: HubDatabase,
    completion_registry: Any | None,
    registry_loop: asyncio.AbstractEventLoop | None,
) -> ManagedHookIngress:
    """Validate exact run identity and finalize provisional resume ownership."""
    session_id = event.metadata.get("_platform_session_id")
    if not isinstance(session_id, str) or not session_id:
        return ManagedHookIngress(accepted=True, managed=False)

    session = session_manager.get(session_id)
    expected_run_id = getattr(session, "agent_run_id", None)
    if not isinstance(expected_run_id, str) or not expected_run_id:
        return ManagedHookIngress(accepted=True, managed=False)

    terminal_context = event.data.get("terminal_context")
    supplied_run_id = (
        terminal_context.get("gobby_agent_run_id") if isinstance(terminal_context, dict) else None
    )
    if not isinstance(supplied_run_id, str) or not _is_uuid(supplied_run_id):
        return ManagedHookIngress(
            accepted=False,
            managed=True,
            ambiguous=True,
            reason="managed hook is missing an exact gobby_agent_run_id",
        )
    if supplied_run_id != expected_run_id:
        return ManagedHookIngress(
            accepted=False,
            managed=True,
            run_id=supplied_run_id,
        )

    run = agent_run_manager.get(expected_run_id)
    if run is None:
        raise AgentRunIngressRetryableError(
            session_id=session_id,
            expected_run_id=expected_run_id,
            reason="managed hook run is not durable yet",
        )

    raw_metadata = getattr(run, "resume_metadata_json", None)
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    phase = metadata.get("daemon_stop_resume_phase")
    if phase in _PROVISIONAL_RESUME_PHASES:
        original_run_id = metadata.get("resumed_from_run_id")
        if not isinstance(original_run_id, str) or not _is_uuid(original_run_id):
            raise AgentRunIngressRetryableError(
                session_id=session_id,
                expected_run_id=expected_run_id,
                reason="provisional resume is missing its original run identity",
            )
        try:
            finalize_resume_handoff_threadsafe(
                database,
                original_run_id=original_run_id,
                successor_run_id=expected_run_id,
                child_session_id=session_id,
                completion_registry=completion_registry,
                registry_loop=registry_loop,
            )
        except Exception as exc:
            raise AgentRunIngressRetryableError(
                session_id=session_id,
                expected_run_id=expected_run_id,
                reason=f"provisional resume finalization failed: {type(exc).__name__}",
            ) from exc

    return ManagedHookIngress(accepted=True, managed=True, run_id=expected_run_id)


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False
