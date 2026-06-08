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

# Plan-decision gate timeout for tool-plan managed CLIs (Droid ExitSpecMode),
# mirroring the native ExitPlanMode gate (servers/chat_session_permissions.py).
# On timeout the decision defaults to reject so the CLI stays in plan mode.
MANAGED_PLAN_DECISION_TIMEOUT_SECONDS = 600.0


class ManagedWebChatPermissionsMixin:
    """Permission and plan helpers for managed (non-SDK) web-chat sessions.

    Composing sessions must provide conversation_id/chat_mode strings plus the
    pending question, approval, plan, and mode-persistence attributes annotated
    below; callback attributes are optional and may be None. ``plan_auto_switch``
    defaults to False for managed CLIs unless a backend explicitly overrides it.
    """

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
    # True when the pinned plan came from a structured plan-exit tool argument
    # (e.g. Droid ExitSpecMode) rather than accumulated assistant prose. A
    # structured plan is authoritative and prose never clobbers it.
    _pending_plan_structured: bool
    _pending_plan_allowed_prompts: list[str] | None
    _pending_post_plan_mode: str | None
    _on_mode_persist: Callable[[str], None] | None
    _pending_approval: PendingApproval | None
    _pending_approval_decision: str | None
    _pending_approval_event: asyncio.Event | None
    # Blocking plan-decision gate for tool-plan CLIs (Droid ExitSpecMode). The
    # plan-exit tool parks on _pending_plan_event while the web UI shows
    # plan_pending_approval; provide_plan_decision() releases it. Text-plan CLIs
    # (Codex / ACP / Droid-prose) never set the event and so never block.
    _pending_plan_event: asyncio.Event | None
    _pending_plan_decision: str | None

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
            self._pending_plan_structured = False
            self._pending_plan_allowed_prompts = None
            self._pending_post_plan_mode = None
        else:
            # Leaving plan mode: keep _pending_post_plan_mode so the following
            # sync_sdk_permission_mode() can tell a plan approval (handler sets
            # it) from a manual mode switch (does not).
            self._plan_approved = False
            self._plan_feedback = None
            self._pending_plan_content = None
            self._pending_plan_structured = False
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
        # Release a blocking plan-exit tool (Droid ExitSpecMode) if one is
        # parked; a harmless no-op for text-plan CLIs (the event is None).
        self._pending_plan_decision = decision
        if self._pending_plan_event is not None:
            self._pending_plan_event.set()

    async def _wait_for_plan_decision(self, *, timeout: float | None = None) -> str:
        """Block a plan-exit tool until the user approves or requests changes.

        Mirrors the native ExitPlanMode gate: a tool-plan CLI (Droid
        ExitSpecMode) parks its plan-exit tool here while the web UI shows
        plan_pending_approval. :meth:`provide_plan_decision` unblocks it. A
        timeout defaults to ``"deny"`` (reject) so the CLI stays in plan mode
        rather than silently proceeding. ``timeout`` is resolved from the module
        constant at call time when not given, so tests can patch it.
        """
        wait_timeout = MANAGED_PLAN_DECISION_TIMEOUT_SECONDS if timeout is None else timeout
        self._pending_plan_event = asyncio.Event()
        self._pending_plan_decision = None
        try:
            await asyncio.wait_for(self._pending_plan_event.wait(), timeout=wait_timeout)
        except TimeoutError:
            self._pending_plan_decision = "deny"
            logger.warning("Managed plan-decision gate timed out; defaulting to reject")
        decision = self._pending_plan_decision or "deny"
        self._pending_plan_event = None
        self._pending_plan_decision = None
        return decision

    @property
    def has_blocking_plan_decision(self) -> bool:
        """True while a plan-exit tool is parked awaiting the user's decision.

        Marks the tool-plan model (Droid ExitSpecMode blocks the turn) so the
        plan-approval handler skips continuation injection: releasing the
        decision resumes the paused turn natively, the same way native Claude's
        plan_auto_switch resumes its own ExitPlanMode turn.
        """
        return self._pending_plan_event is not None

    @property
    def has_pending_plan(self) -> bool:
        return self._pending_plan_content is not None

    def _clear_pending_plan_prompt(self) -> None:
        """Clear the in-flight plan approval prompt shown in the UI."""
        self._pending_plan_content = None
        self._pending_plan_structured = False
        self._pending_plan_allowed_prompts = None

    def _should_supersede_pending_plan(self, text: str, *, structured: bool) -> bool:
        """Decide whether ``text`` should replace the currently pinned plan.

        The first substantive content in a plan cycle always broadcasts. After
        that, within the same approval cycle (the pin is reset on approve/reject
        via :meth:`_clear_pending_plan_prompt` and on plan-mode entry):

        * A **structured** plan (delivered as a plan-exit tool argument) wins
          over an earlier prose preamble, and a structured plan is never
          clobbered by later prose.
        * **Prose** only supersedes earlier prose when it is genuinely different
          and not shorter — so an early conversational preamble ("Now I have
          enough context to propose a plan.") cannot outrank the longer real
          plan emitted in a later turn, while trailing chatter cannot displace
          an already-pinned plan.

        Shorter revisions are still surfaced: a reject clears the pin first, so
        the revised plan broadcasts via the first-content path regardless of
        length.
        """
        current = self._pending_plan_content
        if current is None:
            return True
        if text == current:
            return False
        if structured:
            # Authoritative: a plan-exit tool argument always supersedes a prose
            # preamble (regardless of length), and a newer structured plan
            # supersedes an older one.
            return True
        if self._pending_plan_structured:
            # Never let later prose clobber an authoritative structured plan.
            return False
        # Prose vs prose: a fuller later turn supersedes a shorter preamble;
        # trailing chatter cannot displace an already-pinned plan.
        return len(text) >= len(current)

    async def _maybe_broadcast_pending_plan(
        self, plan_text: str, saw_content: bool, *, structured: bool = False
    ) -> None:
        """Surface a presented plan to the web UI for managed CLIs.

        Managed providers deliver a plan either as a structured plan-exit tool
        argument (Droid ``ExitSpecMode``) or as a normal assistant turn. When a
        substantive turn completes in plan mode, broadcast plan_pending_approval
        using the same payload shape as the SDK path so it flows through the
        shared frontend surfaces.

        ``structured`` marks ``plan_text`` as authoritative tool-argument
        content; :meth:`_should_supersede_pending_plan` lets it replace a prose
        preamble pinned earlier in the same cycle. Structured callers pass their
        own ``saw_content`` because the plan does not arrive as ``content_delta``
        prose.
        """
        if self.chat_mode != "plan" or self._plan_approved:
            return
        if not saw_content:
            return
        text = plan_text.strip()
        if not text:
            return
        if not self._should_supersede_pending_plan(text, structured=structured):
            return
        self._pending_plan_content = text
        self._pending_plan_structured = structured
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
