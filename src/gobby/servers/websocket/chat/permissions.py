"""Tool approval and plan-mode helpers shared by managed web-chat sessions.

Every non-native web-chat session — i.e. every CLI Gobby wraps rather than
drives through the Claude SDK — needs the same approval/plan-mode UX:
tool-approval gating, plan-mode prompt injection, and the plan-pending /
approve / request-changes pipeline. That UX is identical regardless of the
underlying wire protocol, so it lives here in one place:

- ACP CLIs (Gemini, Grok, Qwen) via ``ACPManagedChatSession``
- Codex via the Codex app-server JSON-RPC (``CodexManagedChatSession``)
- Droid via stream-jsonrpc (``DroidManagedChatSession``)

This used to be two near-identical mixins — ``ACPWebChatPermissionsMixin`` and
a misleadingly named ``GeminiWebChatPermissionsMixin`` (the latter used by
Codex/Droid, never Gemini). They are unified here; the name is protocol-neutral
because the logic is not ACP-, Codex-, or Gemini-specific.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.servers.chat_session_helpers import PendingApproval
from gobby.servers.tool_approvals import approval_key_for_tool

logger = logging.getLogger(__name__)


class ManagedWebChatPermissionsMixin:
    """Permission and plan helpers for managed (non-SDK) web-chat sessions."""

    # ACP CLIs expose no protocol-level mode push (no session/set_mode), so
    # plan approval cannot auto-switch the agent at the protocol level: 1c's
    # fallback only flips the Gobby-side mode + UI radio. The UI uses this to
    # note that a manual switch/continue is required. The Claude SDK session is
    # native and defaults to True (via getattr in the session_info builder).
    plan_auto_switch: bool = False

    conversation_id: str
    chat_mode: str
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None
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
    _last_plan_content: str | None
    _pending_plan_content: str | None
    _pending_plan_allowed_prompts: list[str] | None
    _pending_post_plan_mode: str | None
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
        self._pending_answers = answers
        if self._pending_answer_event is not None:
            self._pending_answer_event.set()

    @property
    def has_pending_question(self) -> bool:
        return self._pending_question is not None

    def set_chat_mode(self, mode: str) -> None:
        self.chat_mode = mode
        if mode == "plan":
            self._plan_approved = False
            self._plan_feedback = None
            self._plan_file_path = None
            self._last_plan_content = None
            self._pending_plan_content = None
            self._pending_plan_allowed_prompts = None
            self._pending_post_plan_mode = None
        else:
            # Leaving plan mode: keep _pending_post_plan_mode so the following
            # sync_sdk_permission_mode() can tell a plan approval (handler sets
            # it) from a manual mode switch (does not).
            self._plan_approved = False
            self._plan_feedback = None
            self._pending_plan_content = None
            self._pending_plan_allowed_prompts = None
        if self._on_mode_persist:
            try:
                self._on_mode_persist(mode)
            except (OSError, ValueError) as e:
                logger.warning(f"Failed to persist chat_mode={mode}: {e}")

    def approve_plan(self) -> None:
        self._plan_approved = True

    def set_plan_feedback(self, feedback: str) -> None:
        self._plan_feedback = feedback

    def provide_plan_decision(self, decision: str) -> None:
        if decision == "approve":
            self._plan_approved = True

    @property
    def has_pending_plan(self) -> bool:
        return self._pending_plan_content is not None

    def _clear_pending_plan_prompt(self) -> None:
        """Clear the in-flight plan approval prompt shown in the UI."""
        self._pending_plan_content = None
        self._pending_plan_allowed_prompts = None

    async def _maybe_broadcast_pending_plan(self, plan_text: str, saw_content: bool) -> None:
        """Surface a presented plan to the web UI for managed CLIs.

        Managed providers have no ExitPlanMode tool, so a plan is delivered as a
        normal assistant turn. When a substantive turn completes in plan mode,
        broadcast plan_pending_approval using the same payload shape as the SDK
        path so it flows through the shared frontend surfaces.
        """
        if self.chat_mode != "plan" or self._plan_approved:
            return
        if not saw_content:
            return
        text = plan_text.strip()
        if not text:
            return
        if self._pending_plan_content is not None:
            return
        self._pending_plan_content = text
        self._last_plan_content = text
        if self._on_plan_ready is not None:
            await self._on_plan_ready(text, {"plan": text})

    def _pop_plan_mode_context(self) -> str | None:
        if self.chat_mode != "plan":
            return None

        if self._plan_approved:
            return (
                '<plan-mode status="approved">\n'
                "The user has approved your plan, but you are still in PLAN MODE.\n"
                "Do not execute changes until the session is explicitly switched to YOLO.\n"
                "</plan-mode>"
            )

        parts = [
            '<plan-mode status="active">',
            "You are in PLAN MODE. Your role is to research and design, not execute.",
            "",
            "ALLOWED: Read, Glob, Grep, gcode via Bash for code navigation (gcode outline/search/symbol), read-only Bash (ls, cat, grep, git status/log/diff, find)",
            "BLOCKED: Edit, Write, NotebookEdit, write/destructive Bash/exec_command",
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

    async def _wait_for_tool_approval(
        self, tool_name: str, input_data: dict[str, Any]
    ) -> dict[str, Any]:
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
            self._approved_tools.add(approval_key_for_tool(tool_name, input_data))
            if self._on_approved_tools_persist:
                self._on_approved_tools_persist(self._approved_tools)
            return {"decision": "accept"}

        if decision == "approve":
            return {"decision": "accept"}

        return {"decision": "decline", "reason": f"User rejected tool call: {tool_name}"}

    def provide_approval(self, decision: str) -> None:
        self._pending_approval_decision = decision
        if self._pending_approval_event is not None:
            self._pending_approval_event.set()

    async def sync_sdk_permission_mode(self) -> None:
        """Apply the post-plan mode transition for managed CLIs.

        Managed CLIs expose no protocol-level mode push (no session/set_mode),
        so on plan approval the user-visible fallback is to broadcast the
        Gobby-side mode change. The plan gate stops re-injecting because
        chat_mode is no longer "plan" (see _pop_plan_mode_context).

        _pending_post_plan_mode is set only by the plan-approval handler, which
        distinguishes an approval from a manual mode switch (where this is a
        no-op) and from entering plan mode.
        """
        if self.chat_mode == "plan" or self._pending_post_plan_mode is None:
            return
        self._pending_post_plan_mode = None
        if self._on_mode_changed is not None:
            await self._on_mode_changed(self.chat_mode, "plan_approved")

    @property
    def has_pending_approval(self) -> bool:
        return self._pending_approval is not None


__all__ = ["ManagedWebChatPermissionsMixin"]
