"""Request-scoped caller identity for machine ownership checks.

The binding is a context variable separate from MCP session context.
``asyncio.create_task`` and ``asyncio.to_thread`` copy it into work the
request starts. An unbound context is hub-internal work and skips ownership.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from gobby.hub.node_client import NodeCommandError
from gobby.storage.machines import LocalMachineManager
from gobby.utils.machine_id import require_machine_id


@dataclass(frozen=True, slots=True)
class CallerScope:
    """The user and database bound for the current request, if any."""

    user_id: str | None
    database: Any


_caller_scope: ContextVar[CallerScope | None] = ContextVar("gobby_caller_scope", default=None)


def current_caller() -> CallerScope | None:
    """Return the bound caller, or None when this task is unbound."""
    return _caller_scope.get()


def bind_caller(user_id: str | None, database: Any) -> Token[CallerScope | None]:
    """Bind a caller for this task and the tasks it creates."""
    return _caller_scope.set(CallerScope(user_id=user_id, database=database))


def bind_decision(decision: Any, database: Any) -> Token[CallerScope | None] | None:
    """Bind an allowed decision. Break-glass stays unbound."""
    if getattr(decision, "break_glass", False):
        return None
    user_id = getattr(decision, "user_id", None)
    return bind_caller(user_id if isinstance(user_id, str) else None, database)


def reset_caller(token: Token[CallerScope | None] | None) -> None:
    """Clear a binding. No token means the path stayed unbound."""
    if token is not None:
        _caller_scope.reset(token)


def require_machine_owned(machine_id: str | None) -> None:
    """Allow the call only when the bound user owns the machine."""
    scope = current_caller()
    if scope is None:
        return
    if scope.user_id is None:
        raise NodeCommandError("machine_not_owned", stage="none")
    if scope.database is None:
        raise NodeCommandError("machine_not_found", stage="none")

    resolved = machine_id or require_machine_id()
    machine = LocalMachineManager(scope.database).get(resolved)
    if machine is None:
        raise NodeCommandError("machine_not_found", stage="none")
    if machine.owner_user_id != scope.user_id:
        raise NodeCommandError("machine_not_owned", stage="none")
