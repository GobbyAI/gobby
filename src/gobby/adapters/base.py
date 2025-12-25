"""Base adapter class for CLI hook translation.

This module defines the abstract base class that all CLI adapters must implement.
Adapters are responsible for translating between CLI-specific hook formats and
the unified HookEvent/HookResponse models.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from gobby.hooks.events import HookEvent, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


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
    def translate_to_hook_event(self, native_event: dict) -> HookEvent:
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
    def translate_from_hook_response(self, response: HookResponse) -> dict:
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
        self, native_event: dict, hook_manager: "HookManager"
    ) -> dict:
        """Main entry point for HTTP endpoints.

        This method handles the full round-trip:
        1. Translate native event to HookEvent
        2. Process through HookManager
        3. Translate response back to native format

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
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response)
