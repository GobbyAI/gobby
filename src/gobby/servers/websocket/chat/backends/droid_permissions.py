"""Permission request resolution for Droid JSON-RPC streams."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from gobby.adapters.acp_client import StreamEvent
from gobby.servers.chat_session_helpers import _BASH_WRITE_PATTERNS, _PLAN_FILE_PATTERN
from gobby.servers.tool_approvals import (
    DEFAULT_GLOBAL_APPROVAL_RULES,
    find_out_of_repo_write_path,
    get_global_approval_rules,
    is_gcode_shell_command,
    is_tool_auto_allowed,
    load_project_approval_rules_async,
    normalize_approved_tool_keys,
)
from gobby.servers.websocket.chat.backends.droid_plan import (
    _extract_plan_from_tool_args,
    _is_plan_exit_tool,
)
from gobby.storage.config_store import ConfigStore

DROID_PERMISSION_CANCEL = "cancel"
DROID_PERMISSION_PROCEED_ONCE = "proceed_once"

ToolPayload = tuple[str, dict[str, Any], str]


class DroidPermissionSession(Protocol):
    """Session surface needed by Droid permission resolution."""

    project_path: str | None
    chat_mode: str
    _approved_tools: set[str]
    _session_manager_ref: Any | None
    _plan_exit_blocked_this_turn: bool

    async def _apply_pre_tool_lifecycle(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    async def _maybe_broadcast_pending_plan(
        self, plan_text: str, saw_content: bool, *, structured: bool = False
    ) -> None: ...

    async def _wait_for_plan_decision(self, *, timeout: float | None = None) -> str: ...

    async def _wait_for_tool_approval(
        self, tool_name: str, input_data: dict[str, Any]
    ) -> dict[str, Any]: ...


class DroidPermissionResolver:
    """Resolve Droid permission request events into selected options."""

    def __init__(self, tool_name_adapter: Callable[[str], str]) -> None:
        self._tool_name_adapter = tool_name_adapter

    async def resolve(
        self,
        session: DroidPermissionSession,
        events: list[StreamEvent],
    ) -> str:
        tool_payloads = [self._permission_tool_payload(event) for event in events]
        if not tool_payloads:
            return DROID_PERMISSION_CANCEL

        for tool_name, tool_input, _tool_id in tool_payloads:
            lifecycle_response = await session._apply_pre_tool_lifecycle(tool_name, tool_input)
            if (
                isinstance(lifecycle_response, dict)
                and lifecycle_response.get("decision") == "block"
            ):
                return DROID_PERMISSION_CANCEL

            if find_out_of_repo_write_path(
                tool_name,
                tool_input,
                project_path=session.project_path,
            ):
                return DROID_PERMISSION_CANCEL

        if session.chat_mode == "bypass":
            return DROID_PERMISSION_PROCEED_ONCE

        project_rules = await load_project_approval_rules_async(session.project_path)
        global_rules = self._global_rules_for_session(session)
        session_rules = normalize_approved_tool_keys(session._approved_tools)
        if all(
            is_tool_auto_allowed(
                tool_name,
                tool_input,
                session_rules=session_rules,
                project_rules=project_rules,
                global_rules=global_rules,
            )
            for tool_name, tool_input, _tool_id in tool_payloads
        ):
            return DROID_PERMISSION_PROCEED_ONCE

        if session.chat_mode == "plan":
            if any(
                self._plan_mode_blocks_tool(tool_name, tool_input)
                for tool_name, tool_input, _tool_id in tool_payloads
            ):
                return DROID_PERMISSION_CANCEL
            return await self._resolve_plan_mode_request(session, tool_payloads)

        approval_tool_name, approval_input = self._approval_prompt_payload(tool_payloads)
        approval = await session._wait_for_tool_approval(approval_tool_name, approval_input)
        if isinstance(approval, dict) and approval.get("decision") == "accept":
            return DROID_PERMISSION_PROCEED_ONCE
        return DROID_PERMISSION_CANCEL

    def _permission_tool_payload(self, event: StreamEvent) -> ToolPayload:
        raw_name = event.data.get("tool_name") or event.data.get("name") or "unknown"
        tool_name = self._tool_name_adapter(str(raw_name))
        tool_input = event.data.get("tool_input") or event.data.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_id = event.data.get("call_id") or event.data.get("id") or "unknown"
        return tool_name, tool_input, str(tool_id)

    async def _resolve_plan_mode_request(
        self,
        session: DroidPermissionSession,
        tool_payloads: list[ToolPayload],
    ) -> str:
        # Tool-plan model (#15682): Droid presents its finalized plan via the
        # ExitSpecMode plan-exit tool, riding the spec in the tool input.
        # ExitSpecMode arrives only as a permission request and is filtered out
        # of the session stream, so this resolver is the only place that sees it.
        # Broadcast the spec and block on the user's decision here, mirroring the
        # native ExitPlanMode gate.
        for tool_name, tool_input, _tool_id in tool_payloads:
            if not _is_plan_exit_tool(tool_name):
                continue
            spec = _extract_plan_from_tool_args(tool_input)
            if not spec:
                # No spec body to show; fall through to the cancel below rather
                # than blocking on an invisible plan card.
                break
            await session._maybe_broadcast_pending_plan(spec, True, structured=True)
            session._plan_exit_blocked_this_turn = True
            decision = await session._wait_for_plan_decision()
            if decision == "approve":
                return DROID_PERMISSION_PROCEED_ONCE
            return DROID_PERMISSION_CANCEL

        # Read-only planning mode (#15664): destructive tools were cancelled in
        # the per-tool loop and auto-allowed reads already proceeded. Any
        # non-plan-exit tool still here needs interactive approval, which cannot
        # be granted during the headless plan turn.
        return DROID_PERMISSION_CANCEL

    @staticmethod
    def _global_rules_for_session(session: DroidPermissionSession) -> list[str]:
        session_manager = getattr(session, "_session_manager_ref", None)
        db = getattr(session_manager, "db", None) if session_manager else None
        if db is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(ConfigStore(db))

    @staticmethod
    def _approval_prompt_payload(tool_payloads: list[ToolPayload]) -> tuple[str, dict[str, Any]]:
        if len(tool_payloads) == 1:
            tool_name, tool_input, _tool_id = tool_payloads[0]
            return tool_name, tool_input
        return (
            "DroidToolBatch",
            {
                "tool_uses": [
                    {"tool_name": tool_name, "tool_input": tool_input, "tool_id": tool_id}
                    for tool_name, tool_input, tool_id in tool_payloads
                ]
            },
        )

    @staticmethod
    def _plan_mode_blocks_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name in {"Write", "Edit", "NotebookEdit"}:
            file_path = tool_input.get("file_path", "")
            return (
                not isinstance(file_path, str)
                or not file_path
                or not _PLAN_FILE_PATTERN.match(file_path)
            )
        if tool_name == "Bash":
            if is_gcode_shell_command(tool_input):
                return False
            return bool(_BASH_WRITE_PATTERNS.search(str(tool_input.get("command", ""))))
        return False


__all__ = [
    "DROID_PERMISSION_CANCEL",
    "DROID_PERMISSION_PROCEED_ONCE",
    "DroidPermissionResolver",
]
