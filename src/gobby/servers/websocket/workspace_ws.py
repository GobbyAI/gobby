"""Workspace messages on the terminal WebSocket (plan gclient-workspaces 2.2).

``workspace_attach`` and ``workspace_snapshot`` serve one workspace's rows with
the lifecycle watermark; ``workspace_op`` runs one op of the ``WorkspaceOps``
vocabulary, named ``<scope>.<verb>`` (``pane.split`` runs ``pane_split``). The
socket is authenticated as the local user, so every op runs as the operator
actor and no client field can name another. Fields are checked against the op's
own signature, so this surface cannot drift from the ops module.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Any, Final, get_args, get_type_hints

from gobby.servers.websocket.terminal_ws_create import _bounded_code
from gobby.storage.workspaces import Workspace
from gobby.terminals.actor_scope import OPERATOR_ACTOR
from gobby.terminals.leases import LifecyclePublicationError
from gobby.terminals.workspace_ops import WorkspaceOpError, WorkspaceOps, WorkspaceSnapshot
from gobby.utils.datetime import to_json_safe

if TYPE_CHECKING:
    from gobby.terminals.leases import TerminalLeaseRegistry

_ENVELOPE: Final = frozenset({"type", "request_id"})
_OP_ENVELOPE: Final = _ENVELOPE | {"op"}


@dataclass(frozen=True, slots=True)
class _Field:
    types: tuple[type, ...]
    required: bool


def _fields(method: Callable[..., Any]) -> dict[str, _Field]:
    hints = get_type_hints(method)
    fields: dict[str, _Field] = {}
    for name, param in inspect.signature(method).parameters.items():
        if name in {"self", "actor"}:
            continue
        hint = hints[name]
        types = get_args(hint) if isinstance(hint, UnionType) else (hint,)
        # Only plain classes check with isinstance; fail at import, not per message.
        if not all(isinstance(option, type) for option in types):
            raise TypeError(f"WorkspaceOps.{method.__name__}.{name}: unsupported type {hint}")
        fields[name] = _Field(types, param.default is param.empty)
    return fields


WORKSPACE_OPS: Final[dict[str, str]] = {
    name.replace("_", ".", 1): name
    for name, _method in inspect.getmembers(WorkspaceOps, inspect.iscoroutinefunction)
    if not name.startswith("_") and name != "workspace_snapshot"
}
_SIGNATURES: Final[dict[str, dict[str, _Field]]] = {
    name: _fields(getattr(WorkspaceOps, name))
    for name in (*WORKSPACE_OPS.values(), "workspace_snapshot")
}


def _fits(option: type, value: object) -> bool:
    if isinstance(value, bool):
        return option is bool
    return isinstance(value, int | float) if option is float else isinstance(value, option)


def _arguments(method: str, data: Mapping[str, Any], envelope: frozenset[str]) -> dict[str, Any]:
    fields = _SIGNATURES[method]
    unknown = sorted(set(data) - envelope - set(fields))
    if unknown:
        raise WorkspaceOpError("invalid_op", f"Unknown fields: {', '.join(unknown)}")
    arguments: dict[str, Any] = {}
    for name, field in fields.items():
        if name not in data:
            if field.required:
                raise WorkspaceOpError("invalid_op", f"Missing field: {name}")
            continue
        if not any(_fits(option, data[name]) for option in field.types):
            expected = " or ".join(
                "null" if option is NoneType else option.__name__ for option in field.types
            )
            raise WorkspaceOpError("invalid_op", f"Field {name} must be {expected}")
        arguments[name] = data[name]
    return arguments


def _result(value: object) -> Any:
    if isinstance(value, tuple):
        return [_result(item) for item in value]
    if isinstance(value, Workspace):
        return to_json_safe(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_safe(asdict(value))
    return value


class WorkspaceWsMixin:
    """Serve ``workspace_attach``, ``workspace_snapshot``, and ``workspace_op``."""

    workspace_ops: WorkspaceOps | None
    shutdown_in_progress: Callable[[], bool]

    if TYPE_CHECKING:

        async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None: ...

        def _leases(self) -> TerminalLeaseRegistry: ...

    async def _handle_workspace_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        try:
            self._ensure_workspace_requests_open()
            snapshot = await self._read_workspace(data)
        except LifecyclePublicationError:
            if not self.shutdown_in_progress():
                raise
            await self._send_workspace_error(websocket, data, self._shutdown_error())
            return
        except WorkspaceOpError as exc:
            await self._send_workspace_error(websocket, data, exc)
            return
        if getattr(websocket, "subscriptions", None) is None:
            websocket.subscriptions = set()
        websocket.subscriptions.add(f"workspace_event:workspace_id={snapshot.workspace.id}")
        await self._send_json(websocket, self._snapshot_reply(data, snapshot))

    async def _handle_workspace_snapshot(self, websocket: Any, data: dict[str, Any]) -> None:
        try:
            self._ensure_workspace_requests_open()
            snapshot = await self._read_workspace(data)
        except LifecyclePublicationError:
            if not self.shutdown_in_progress():
                raise
            await self._send_workspace_error(websocket, data, self._shutdown_error())
            return
        except WorkspaceOpError as exc:
            await self._send_workspace_error(websocket, data, exc)
            return
        await self._send_json(websocket, self._snapshot_reply(data, snapshot))

    async def _handle_workspace_op(self, websocket: Any, data: dict[str, Any]) -> None:
        op = data.get("op")
        try:
            self._ensure_workspace_requests_open()
            method = WORKSPACE_OPS.get(op) if isinstance(op, str) else None
            if method is None:
                raise WorkspaceOpError("invalid_op", f"Unknown workspace op: {op!r}")
            arguments = _arguments(method, data, _OP_ENVELOPE)
            result = await getattr(self._workspace_ops(), method)(OPERATOR_ACTOR, **arguments)
        except LifecyclePublicationError:
            if not self.shutdown_in_progress():
                raise
            await self._send_workspace_error(websocket, data, self._shutdown_error())
            return
        except WorkspaceOpError as exc:
            await self._send_workspace_error(websocket, data, exc)
            return
        await self._send_json(
            websocket,
            {
                "type": "workspace_op",
                "request_id": data.get("request_id"),
                "op": op,
                "result": _result(result),
            },
        )

    def _workspace_ops(self) -> WorkspaceOps:
        if self.workspace_ops is None:
            raise WorkspaceOpError("terminal_failed", "Workspace ops are not configured")
        return self.workspace_ops

    def _ensure_workspace_requests_open(self) -> None:
        if self.shutdown_in_progress():
            raise self._shutdown_error()

    @staticmethod
    def _shutdown_error() -> WorkspaceOpError:
        return WorkspaceOpError("shutdown_in_progress", "Daemon is shutting down")

    async def _read_workspace(self, data: dict[str, Any]) -> WorkspaceSnapshot:
        arguments = _arguments("workspace_snapshot", data, _ENVELOPE)
        return await self._workspace_ops().workspace_snapshot(OPERATOR_ACTOR, **arguments)

    def _snapshot_reply(self, data: dict[str, Any], snapshot: WorkspaceSnapshot) -> dict[str, Any]:
        # Called before any await after the rows were read, so no lifecycle event can
        # commit between them: events above the watermark are all news to the rows.
        return {
            "type": "workspace_snapshot",
            "request_id": data.get("request_id"),
            "workspace": {**_result(snapshot.workspace), "node_ref": snapshot.node.ref},
            "tabs": _result(snapshot.tabs),
            "panes": _result(snapshot.panes),
            "snapshot": self._leases().lifecycle_snapshot(),
        }

    async def _send_workspace_error(
        self, websocket: Any, data: dict[str, Any], exc: WorkspaceOpError
    ) -> None:
        await self._send_json(
            websocket,
            {
                "type": "workspace_error",
                "request_id": data.get("request_id"),
                "code": _bounded_code(exc.code, "invalid_op"),
                "reason": str(exc),
            },
        )
