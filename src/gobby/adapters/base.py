"""Base adapter class for CLI hook translation.

This module defines the abstract base class that all CLI adapters must implement.
Adapters are responsible for translating between CLI-specific hook formats and
the unified HookEvent/HookResponse models.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


module_logger = logging.getLogger(__name__)

ADAPTER_EMPTY_BLOCK_REASON_SENTINEL = (
    "Blocked by hook (ghook fallback — no reason forwarded; file a bug)"
)


def system_message_has_session_banner(system_message: str | None) -> bool:
    """Return whether a system message already includes the Gobby session banner."""
    return isinstance(system_message, str) and "Gobby Session ID:" in system_message


def build_first_hook_session_metadata_lines(
    metadata: Mapping[str, Any],
    *,
    include_session_id_line: bool = True,
    include_task_id: bool = True,
    include_tty: bool = True,
) -> list[str]:
    """Build first-hook session metadata lines for startup context injection."""
    session_id = metadata.get("session_id")
    is_first_hook = metadata.get("_first_hook_for_session", False)
    if not session_id or not is_first_hook:
        return []

    session_ref = metadata.get("session_ref")
    external_id = metadata.get("external_id")
    lines: list[str] = []

    if include_session_id_line:
        if session_ref:
            lines.append(f"Gobby Session ID: {session_ref} ({session_id})")
        else:
            lines.append(f"Gobby Session ID: {session_id}")

    if external_id:
        lines.append(f"CLI-Specific Session ID (external_id): {external_id}")
    if metadata.get("parent_session_id"):
        lines.append(f"parent_session_id: {metadata['parent_session_id']}")
    if metadata.get("machine_id"):
        lines.append(f"machine_id: {metadata['machine_id']}")
    if metadata.get("project_id"):
        lines.append(f"project_id: {metadata['project_id']}")
    if include_task_id and metadata.get("task_id"):
        lines.append(
            f"Assigned Task: {metadata['task_id']}"
            " (use this for task operations, NOT the session ID above)"
        )
    if metadata.get("terminal_term_program"):
        lines.append(f"terminal: {metadata['terminal_term_program']}")
    if include_tty and metadata.get("terminal_tty"):
        lines.append(f"tty: {metadata['terminal_tty']}")
    if metadata.get("terminal_parent_pid"):
        lines.append(f"parent_pid: {metadata['terminal_parent_pid']}")
    if metadata.get("terminal_tmux_pane"):
        lines.append(f"tmux pane: {metadata['terminal_tmux_pane']}")

    return lines


def normalize_adapter_response_reason(
    response: HookResponse,
    *,
    adapter_name: str,
    hook_type: str | None,
    logger: logging.Logger | None = None,
) -> str | None:
    """Return trimmed reason text, or a loud sentinel for blank block/deny responses."""
    reason = response.reason.strip() if isinstance(response.reason, str) else None
    if reason:
        return reason

    if response.decision not in {"deny", "block"}:
        return None

    (logger or module_logger).warning(
        "%s translated %s without reason at adapter boundary; "
        "using ghook fallback sentinel for hook_type=%s response=%s",
        adapter_name,
        response.decision,
        hook_type or "unknown",
        asdict(response),
    )
    return ADAPTER_EMPTY_BLOCK_REASON_SENTINEL


class BaseAdapter(ABC):
    """Base class for CLI adapters that translate native events to HookEvents.

    Each CLI (Claude Code, Gemini, Codex) has its own adapter that:
    1. Knows how to parse the CLI's native hook payload format
    2. Translates payloads to unified HookEvent objects
    3. Translates HookResponse objects back to CLI-expected format

    Subclasses must implement:
    - source: The SessionSource enum value for this CLI
    - translate_to_hook_event(): Convert native payload to HookEvent
    - translate_from_hook_response(): Convert HookResponse to native format
    """

    source: SessionSource

    @abstractmethod
    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent | None:
        """Convert native CLI event to unified HookEvent.

        Args:
            native_event: The raw payload from the CLI's hook dispatcher.
                Structure varies by CLI:
                - Claude Code: {"hook_type": "...", "input_data": {...}}
                - Gemini: {"hook_event_name": "...", "session_id": "...", ...}
                - Codex: JSON-RPC params from app-server events

        Returns:
            A unified HookEvent that can be processed by HookManager.
        """
        pass

    @abstractmethod
    def translate_from_hook_response(self, response: HookResponse) -> dict[str, Any]:
        """Convert HookResponse to native CLI response format.

        Args:
            response: The unified HookResponse from HookManager.

        Returns:
            A dict in the format expected by the CLI's hook dispatcher:
            - Claude Code: {"continue": bool, "stopReason": str | None, ...}
            - Gemini: {"decision": str, "hookSpecificOutput": {...}}
            - Codex: JSON-RPC response format
        """
        pass

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoints.

        This method handles the full round-trip:
        1. Translate native event to HookEvent
        2. Inject daemon's machine_id if not provided by CLI
        3. Process through HookManager
        4. Translate response back to native format

        Note: This method is synchronous for Phase 2A-2B compatibility.
        In Phase 2C+, when HookManager.handle() is async, subclasses may
        override with async versions.

        Subclasses may override this to add CLI-specific behavior, such as
        the strangler fig pattern used by ClaudeCodeAdapter.

        Args:
            native_event: The raw payload from the CLI.
            hook_manager: The HookManager instance to process events.

        Returns:
            Response dict in CLI-specific format.
        """
        hook_event = self.translate_to_hook_event(native_event)
        if hook_event is None:
            # Event ignored by adapter
            return {}

        # Inject daemon's machine_id if CLI didn't provide it
        # This centralizes machine_id handling - adapters don't generate IDs
        if not hook_event.machine_id:
            from gobby.utils.machine_id import get_machine_id

            hook_event.machine_id = get_machine_id()

        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response)
