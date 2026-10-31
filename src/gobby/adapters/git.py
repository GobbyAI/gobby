"""Git adapter: the protected-branch guard's synchronous PreToolUse verdict.

The reference-transaction guard posts one synthetic PreToolUse for each update to an
integration branch. Only an explicit allow lands; every other outcome is a block, which
ghook turns into a refusal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter, normalize_adapter_response_reason
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

# The guard only judges shell commits, so the synthetic event always names Bash.
GIT_TOOL_NAME = "Bash"
GIT_DEFAULT_COMMAND = "git commit"
GIT_BLOCK_FALLBACK_REASON = "Gobby refused this integration-branch update."


class GitAdapter(BaseAdapter):
    """Translate the guard payload and map the HookManager verdict to allow or block."""

    def __init__(self, hook_manager: HookManager | None = None) -> None:
        self._hook_manager = hook_manager

    @property
    def source(self) -> SessionSource:
        return SessionSource.GIT

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Build a BEFORE_TOOL event from the guard's input_data."""
        raw_input = native_event.get("input_data")
        input_data = raw_input if isinstance(raw_input, dict) else {}

        platform_session_id = native_event.get("_platform_session_id")
        external_session_id = input_data.get("session_id")
        if isinstance(platform_session_id, str) and platform_session_id:
            session_id = platform_session_id
        elif isinstance(external_session_id, str):
            session_id = external_session_id
        else:
            session_id = ""

        staged = input_data.get("staged_paths")
        staged_paths: list[str] = []
        if isinstance(staged, list):
            staged_paths = [path for path in staged if isinstance(path, str)]
        branch = input_data.get("integration_branch")
        tool_input = input_data.get("tool_input")
        cwd = input_data.get("cwd")
        machine_id = input_data.get("machine_id")

        data: dict[str, Any] = {
            "tool_name": GIT_TOOL_NAME,
            "tool_input": (
                tool_input if isinstance(tool_input, dict) else {"command": GIT_DEFAULT_COMMAND}
            ),
            "staged_paths": staged_paths,
            "integration_branch": branch if isinstance(branch, str) and branch else None,
            "cwd": cwd if isinstance(cwd, str) else None,
        }
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=session_id,
            source=self.source,
            timestamp=self._hook_event_timestamp(native_event),
            data=data,
            machine_id=machine_id if isinstance(machine_id, str) else None,
            cwd=data["cwd"],
            metadata=self._copy_platform_session_metadata(native_event, {}),
        )

    def translate_from_hook_response(
        self,
        response: HookResponse,
        hook_type: str | None = None,
    ) -> dict[str, Any]:
        """Allow only an explicit allow; every other decision refuses the update."""
        if response.decision == "allow" and response.permission_decision != "deny":
            return {"decision": "allow"}
        reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
        )
        return {"decision": "block", "reason": reason or GIT_BLOCK_FALLBACK_REASON}
