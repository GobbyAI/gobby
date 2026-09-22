"""Terminal create and kill handlers split from the websocket monolith."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.config.terminals import TerminalConfig
from gobby.servers.websocket.tmux_activation import TmuxAttachHost, teardown_terminal_bridges
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.termination import kill_terminal
from gobby.terminals.ws_protocol import inventory_item

if TYPE_CHECKING:
    from gobby.terminals.web_spawn import WebSpawnResult

logger = logging.getLogger(__name__)

MAX_RESULT_CODE_LENGTH = 128


def _bounded_code(code: str | None, fallback: str) -> str:
    return (code or fallback)[:MAX_RESULT_CODE_LENGTH]


class TerminalCreateMixin:
    """Serve terminal lifecycle mutations for websocket hosts."""

    terminal_config: Any
    terminal_manager: Any
    terminal_runtime_registry: Any

    if TYPE_CHECKING:

        async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None: ...

        async def broadcast_tmux_session_event(
            self,
            event: str,
            terminal_id: str = "",
            session_name: str | None = None,
            socket: str | None = None,
            terminal: dict[str, Any] | None = None,
        ) -> None: ...

        def _project_id(self, websocket: Any) -> str | None: ...

    async def _handle_terminal_create(self, websocket: Any, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        try:
            validate_dimensions(data.get("rows"), data.get("cols"))
        except InvalidTerminalDimensionsError:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "code": "invalid_dimensions",
                    "request_id": request_id,
                },
            )
            return
        from gobby.terminals.web_spawn import spawn_web_terminal

        manager = getattr(self, "terminal_manager", None)
        registry = getattr(self, "terminal_runtime_registry", None)
        project_id = data.get("project_id") or self._project_id(websocket) or GLOBAL_PROJECT_ID
        if manager is None or registry is None or not isinstance(project_id, str):
            code, reason = self._unavailable_create_service(manager, registry, project_id)
            logger.warning("terminal_create refused: %s", reason)
            await self._send_json(
                websocket,
                {
                    "type": "terminal_create_result",
                    "success": False,
                    "request_id": request_id,
                    "code": code,
                    "reason": reason,
                },
            )
            return
        backend = getattr(self.terminal_config, "default_backend", None)
        try:
            runtime = registry.resolve(backend or TerminalConfig().default_backend)
        except Exception as exc:
            reason = str(exc) or "terminal runtime unavailable"
            await self._send_json(
                websocket,
                {
                    "type": "terminal_create_result",
                    "success": False,
                    "request_id": request_id,
                    "code": "runtime_unavailable",
                    "reason": reason,
                },
            )
            return
        command = data.get("command") or ["zsh"]
        if not isinstance(command, list):
            command = ["zsh"]
        result: WebSpawnResult = await spawn_web_terminal(
            manager=manager,
            runtime=runtime,
            project_id=project_id,
            session_id=None,
            rows=data.get("rows"),
            cols=data.get("cols"),
            cwd=data.get("cwd"),
            command=[str(part) for part in command],
        )
        if not result.success:
            logger.warning(
                "terminal_create spawn failed (backend=%s, terminal=%s): %s",
                runtime.backend,
                result.terminal_id,
                result.error,
            )
        payload: dict[str, Any] = {
            "type": "terminal_create_result",
            "request_id": request_id,
            "success": result.success,
            "terminal_id": result.terminal_id,
            "backend": runtime.backend,
            "reason": result.error_detail or result.error,
        }
        if not result.success:
            payload["code"] = _bounded_code(result.error, "spawn_failed")
        await self._send_json(websocket, payload)
        if result.success:
            row = manager.get(result.terminal_id)
            if row is not None:
                await self.broadcast_tmux_session_event(
                    "created",
                    terminal_id=result.terminal_id,
                    terminal=inventory_item(row),
                )

    @staticmethod
    def _unavailable_create_service(
        manager: object,
        registry: object,
        project_id: object,
    ) -> tuple[str, str]:
        if manager is None:
            return "terminal_manager_unavailable", "terminal manager unavailable"
        if registry is None:
            return "terminal_runtime_unavailable", "terminal runtime registry unavailable"
        if not isinstance(project_id, str):
            return "project_unresolved", "project unresolved"
        raise AssertionError("all terminal create services are available")

    async def _handle_terminal_kill(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = data.get("terminal_id")
        if isinstance(terminal_id, str):
            await teardown_terminal_bridges(cast(TmuxAttachHost, self), terminal_id)
        manager = getattr(self, "terminal_manager", None)
        row = (
            None
            if manager is None or not isinstance(terminal_id, str)
            else manager.get(terminal_id)
        )
        transitioned = None
        failure: str | None = None
        if (
            row is not None
            and manager is not None
            and getattr(self, "terminal_runtime_registry", None) is not None
            and row.state in {"live", "orphaned"}
        ):
            # A runtime failure must still answer the request: an escaped
            # exception left the client waiting on a reply that never came.
            try:
                transitioned = await kill_terminal(manager, self.terminal_runtime_registry, row)
            except Exception as exc:
                logger.warning("terminal_kill failed for %s", row.id, exc_info=True)
                failure = str(exc) or type(exc).__name__
            if transitioned is not None:
                await self.broadcast_tmux_session_event("killed", terminal_id=row.id)
        payload: dict[str, Any] = {
            "type": "terminal_kill_result",
            "success": transitioned is not None,
            "terminal_id": terminal_id,
            "request_id": data.get("request_id"),
        }
        if failure is not None:
            payload.update(code="kill_failed", reason=failure)
        elif transitioned is None:
            payload.update(code="terminal_not_live", reason="terminal is not live")
        await self._send_json(websocket, payload)
