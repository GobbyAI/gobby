"""
Tool permission, approval, and plan mode logic for GeminiCLIChatSession.

Mirrors CodexChatSessionPermissionsMixin -- returns Gemini-compatible dicts.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.servers.chat_session_helpers import (
    PendingApproval,
)

logger = logging.getLogger(__name__)


class GeminiCLIChatSessionPermissionsMixin:
    """Tool permission, approval, and plan mode logic for GeminiCLIChatSession.

    Returns decision dicts:
    {"decision": "accept"} or {"decision": "decline", "reason": "..."}

    Attributes set by the concrete dataclass (declared here for type-checking)."""

    # Attribute type stubs -- actual fields live on GeminiCLIChatSession dataclass
    conversation_id: str
    chat_mode: str
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _pending_question: dict[str, Any] | None
    _pending_answers: dict[str, str] | None
    _pending_answer_event: asyncio.Event | None
    _approved_tools: set[str]
    _on_approved_tools_persist: Callable[[set[str]], None] | None
    _tool_approval_config: Any | None
    _tool_approval_callback: Any | None
    _plan_approved: bool
    _plan_feedback: str | None
    _plan_file_path: str | None
    _on_mode_persist: Callable[[str], None] | None
    _pending_approval: PendingApproval | None
    _pending_approval_decision: str | None
    _pending_approval_event: asyncio.Event | None

    _DANGEROUS_BASH_PATTERNS = re.compile(
        r"(?:^|[;&|]\s*)(?:sudo|rm|chmod|chown|kill|killall|mkfs|dd|reboot|shutdown|halt|"
        r"systemctl|service|init|"
        r"mv\s+/|>\s*/|git\s+(?:push|reset\s+--hard|clean\s+-f))\b"
        r"|(?:curl|wget)\s+.*\|\s*(?:ba)?sh\b",
        re.MULTILINE,
    )

    def provide_answer(self, answers: dict[str, str]) -> None:
        """Provide answers to a pending question."""
        self._pending_answers = answers
        if self._pending_answer_event is not None:
            self._pending_answer_event.set()

    @property
    def has_pending_question(self) -> bool:
        return self._pending_question is not None

    def set_chat_mode(self, mode: str) -> None:
        """Set chat mode, resetting plan state when entering plan mode."""
        self.chat_mode = mode
        if mode == "plan":
            self._plan_approved = False
            self._plan_feedback = None
            self._plan_file_path = None
        else:
            self._plan_approved = False
            self._plan_feedback = None
        if self._on_mode_persist:
            try:
                self._on_mode_persist(mode)
            except (OSError, ValueError) as e:
                logger.warning(f"Failed to persist chat_mode={mode}: {e}")

    def approve_plan(self) -> None:
        """Mark the current plan as approved, unlocking write tools."""
        self._plan_approved = True

    def set_plan_feedback(self, feedback: str) -> None:
        """Store user feedback for plan revision."""
        self._plan_feedback = feedback

    def provide_plan_decision(self, decision: str) -> None:
        """Provide plan approval decision."""
        if decision == "approve":
            self.set_chat_mode("accept_edits")
            self._plan_approved = True

    @property
    def has_pending_plan(self) -> bool:
        """Whether a plan is awaiting approval."""
        return False

    def _pop_plan_mode_context(self) -> str | None:
        """Return plan mode system context for injection and clear consumed feedback."""
        if self.chat_mode != "plan":
            return None

        if self._plan_approved:
            return (
                '<plan-mode status="approved">\n'
                "The user has approved your plan. You may now execute it.\n"
                "Write tools (Edit, Write, NotebookEdit, write Bash) are unblocked.\n"
                "</plan-mode>"
            )

        parts = [
            '<plan-mode status="active">',
            "You are in PLAN MODE. Your role is to research and design, not execute.",
            "",
            "ALLOWED: Read, Glob, Grep, read-only Bash (ls, cat, grep, git status/log/diff, find)",
            "BLOCKED: Edit, Write, NotebookEdit, write/destructive Bash",
            "",
            "Present a structured plan with:",
            "1. Summary of changes needed",
            "2. Files to modify and what changes to make",
            "3. Implementation order",
            "4. Verification steps",
            "",
            "When your plan is complete, present it to the user.",
            "The user will approve or request changes via the chat UI.",
        ]

        if self._plan_feedback:
            parts.append("")
            parts.append(f"USER FEEDBACK on previous plan:\n{self._plan_feedback}")
            self._plan_feedback = None

        parts.append("</plan-mode>")
        return "\n".join(parts)

    def _consume_plan_mode_context(self) -> str | None:
        """Backward-compatible alias for callers still using the old name."""
        return self._pop_plan_mode_context()

    async def _wait_for_tool_approval(
        self, tool_name: str, input_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Block until the user approves or rejects a tool call."""
        self._pending_approval = {
            "tool_name": tool_name,
            "arguments": input_data,
        }
        self._pending_approval_decision = None
        self._pending_approval_event = asyncio.Event()

        if self._tool_approval_callback:
            await self._tool_approval_callback(tool_name, input_data)

        try:
            await asyncio.wait_for(self._pending_approval_event.wait(), timeout=300.0)
        except TimeoutError:
            self._pending_approval_decision = "reject"

        decision = self._pending_approval_decision or "reject"

        self._pending_approval = None
        self._pending_approval_event = None
        self._pending_approval_decision = None

        if decision == "reject":
            return {"decision": "decline", "reason": f"User rejected tool call: {tool_name}"}

        if decision == "approve_always":
            self._approved_tools.add(tool_name)
            if self._on_approved_tools_persist:
                self._on_approved_tools_persist(self._approved_tools)
            return {"decision": "accept"}

        if decision == "approve":
            return {"decision": "accept"}

        return {"decision": "decline", "reason": f"User rejected tool call: {tool_name}"}

    def provide_approval(self, decision: str) -> None:
        """Provide approval decision for a pending tool call."""
        self._pending_approval_decision = decision
        if self._pending_approval_event is not None:
            self._pending_approval_event.set()

    async def sync_sdk_permission_mode(self) -> None:
        """No-op for Gemini -- permission mode is enforced via context injection."""
        pass

    @property
    def has_pending_approval(self) -> bool:
        return self._pending_approval is not None
