"""
Tool permission, approval, and plan mode logic for ChatSession.

Extracted as a mixin class to keep chat_session.py under the 1,000-line
guideline. ChatSession inherits from ChatSessionPermissionsMixin.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    _PLAN_FILE_PATTERN,
    _PLAN_MODE_BLOCKED_TOOLS,
    PendingApproval,
)

logger = logging.getLogger(__name__)


class ChatSessionPermissionsMixin:
    """Tool permission, approval, and plan mode logic for ChatSession.

    Declares attribute types expected from the concrete ChatSession dataclass.
    """

    # Attribute type stubs — actual fields live on the ChatSession dataclass
    conversation_id: str
    chat_mode: str
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _pending_question: dict[str, Any] | None
    _pending_answers: dict[str, str] | None
    _pending_answer_event: asyncio.Event | None
    _approved_tools: set[str]
    _tool_approval_config: Any | None
    _tool_approval_callback: Any | None
    _plan_approved: bool
    _plan_feedback: str | None
    _plan_approval_completed: bool
    _plan_file_path: str | None
    _pending_plan_event: asyncio.Event | None
    _pending_plan_decision: str | None
    _on_mode_persist: Callable[[str], None] | None
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None
    _pending_approval: PendingApproval | None
    _pending_approval_decision: str | None
    _pending_approval_event: asyncio.Event | None

    # Patterns that indicate dangerous bash commands (used by accept_edits mode)
    _DANGEROUS_BASH_PATTERNS = re.compile(
        r"(?:^|[;&|]\s*)(?:sudo|rm|chmod|chown|kill|killall|mkfs|dd|reboot|shutdown|halt|"
        r"systemctl|service|init|"
        r"mv\s+/|>\s*/|git\s+(?:push|reset\s+--hard|clean\s+-f))\b"
        r"|(?:curl|wget)\s+.*\|\s*(?:ba)?sh\b",
        re.MULTILINE,
    )

    async def _can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Callback for tool permission checks.

        Auto-approves all tools except AskUserQuestion (which blocks
        until the user provides answers via provide_answer()) and tools
        matched by tool_approval policies (which block until approved).
        """
        # Agent-initiated plan mode transitions
        if tool_name == "EnterPlanMode":
            self.set_chat_mode("plan")
            if self._on_mode_changed:
                await self._on_mode_changed("plan", "agent_requested")
            return PermissionResultAllow(updated_input=input_data)
        if tool_name == "ExitPlanMode":
            # Read plan file content if one was written during plan mode
            plan_content = self._read_plan_file()

            if not plan_content:
                return PermissionResultDeny(
                    message="No plan file found. Write your plan to .gobby/plans/*.md before calling ExitPlanMode."
                )

            # Broadcast plan_pending_approval to frontend
            if self._on_plan_ready:
                await self._on_plan_ready(plan_content, input_data)

            # Block until user approves or requests changes
            self._pending_plan_event = asyncio.Event()
            self._pending_plan_decision = None
            try:
                await asyncio.wait_for(self._pending_plan_event.wait(), timeout=600.0)
            except TimeoutError:
                self._pending_plan_decision = "request_changes"
                self._plan_feedback = (
                    "Plan approval timed out — no response received. Please re-submit your plan."
                )
                logger.warning("ExitPlanMode timed out for session %s", self.conversation_id)

            if self._pending_plan_decision is None:
                logger.warning(
                    "ExitPlanMode resolved without explicit decision for session %s — denying",
                    self.conversation_id,
                )
                self._plan_feedback = (
                    self._plan_feedback
                    or "No approval decision received. Please review and approve the plan."
                )

            decision = self._pending_plan_decision or "request_changes"
            self._pending_plan_event = None
            self._pending_plan_decision = None

            if decision == "approve":
                self._plan_approval_completed = True  # Signal streaming loop
                self.set_chat_mode("accept_edits")
                if self._on_mode_changed:
                    await self._on_mode_changed("accept_edits", "plan_approved")
                return PermissionResultAllow(updated_input=input_data)
            else:
                # request_changes — deny the tool so agent stays in plan mode
                feedback = self._plan_feedback or "User requested changes."
                self._plan_feedback = None
                if self.chat_mode == "plan" and self._on_mode_changed:
                    await self._on_mode_changed("plan", "plan_changes_requested")
                return PermissionResultDeny(message=feedback)

        # Plan mode: block write tools until the plan is approved
        if self.chat_mode == "plan" and not self._plan_approved:
            if tool_name in _PLAN_MODE_BLOCKED_TOOLS:
                # Allow writes to plan files (e.g. ~/.claude/plans/*.md)
                if tool_name in ("Write", "Edit"):
                    file_path = input_data.get("file_path", "")
                    if _PLAN_FILE_PATTERN.match(file_path):
                        self._plan_file_path = file_path
                        return PermissionResultAllow(updated_input=input_data)
                return PermissionResultDeny(
                    message=(
                        f"Plan mode is active — {tool_name} is blocked. "
                        "Present your plan to the user for approval before making changes."
                    )
                )
            if tool_name == "Bash" and self._is_write_bash(input_data):
                return PermissionResultDeny(
                    message=(
                        "Plan mode is active — write/destructive Bash commands are blocked. "
                        "Present your plan to the user for approval before making changes."
                    )
                )

        # Fire session-lifecycle evaluation for tool blocking.
        # In the SDK path, the PreToolUse hook's permissionDecision="deny" is not
        # respected because can_use_tool already granted permission. By checking
        # here, we block tools at the SDK permission gate itself.
        if self._on_pre_tool:
            resp = await self._on_pre_tool({"tool_name": tool_name, "tool_input": input_data})
            if resp and resp.get("decision") == "block":
                return PermissionResultDeny(
                    message=resp.get("reason", "Blocked by session lifecycle")
                )

        # Check tool approval (before AskUserQuestion, which has its own flow)
        if tool_name != "AskUserQuestion":
            if self._needs_tool_approval(tool_name):
                return await self._wait_for_tool_approval(tool_name, input_data)
            # In accept_edits mode, auto-approved Bash still needs danger check
            if self.chat_mode == "accept_edits" and tool_name == "Bash":
                if self._is_dangerous_bash(input_data):
                    return await self._wait_for_tool_approval(tool_name, input_data)
            return PermissionResultAllow(updated_input=input_data)

        # Store the pending question and block until answered
        self._pending_question = input_data
        self._pending_answers = None
        self._pending_answer_event = asyncio.Event()

        try:
            await asyncio.wait_for(self._pending_answer_event.wait(), timeout=600.0)
        except TimeoutError:
            self._pending_answers = {"error": "Timed out waiting for user response"}
            logger.warning("AskUserQuestion timed out for session %s", self.conversation_id)

        result = PermissionResultAllow(
            updated_input={
                "questions": input_data.get("questions", []),
                "answers": self._pending_answers,
            }
        )

        # Clear pending state
        self._pending_question = None
        self._pending_answer_event = None
        self._pending_answers = None

        return result

    def provide_answer(self, answers: dict[str, str]) -> None:
        """Provide answers to a pending AskUserQuestion, unblocking the callback."""
        self._pending_answers = answers
        if self._pending_answer_event is not None:
            self._pending_answer_event.set()

    @property
    def has_pending_question(self) -> bool:
        """Whether an AskUserQuestion is currently awaiting a response."""
        return self._pending_question is not None

    def _needs_tool_approval(self, tool_name: str) -> bool:
        """Check if a tool requires user approval based on chat mode and config.

        Mode logic:
        - bypass: Never prompt (auto-approve everything)
        - accept_edits: Auto-approve Edit/Write/NotebookEdit and safe Bash.
          Prompt for dangerous Bash and MCP call_tool.
        - normal: Fall through to ToolApprovalConfig policy checks
        - plan: Fall through to ToolApprovalConfig policy checks
          (plan mode blocking is handled by the workflow engine, not here)
        """
        mode = self.chat_mode

        # Bypass mode: no approvals ever
        if mode == "bypass":
            return False

        # Accept-edits mode: auto-approve edits and safe bash
        if mode == "accept_edits":
            # Already approved this session
            if tool_name in self._approved_tools:
                return False
            # Auto-approve file edit tools
            if tool_name in ("Edit", "Write", "NotebookEdit"):
                return False
            # Bash: auto-approve unless dangerous patterns detected
            # (actual command content check happens in _can_use_tool via input_data)
            # For the approval gate check, we mark Bash as needing approval
            # only when the config says so — the actual danger check is below
            if tool_name == "Bash":
                return False  # Handled by _is_dangerous_bash in _can_use_tool
            # MCP call_tool and other tools: prompt
            return True

        # Normal / plan mode: use ToolApprovalConfig
        config = self._tool_approval_config
        if config is None or not config.enabled:
            return False

        # Already approved this session (approve_always)
        if tool_name in self._approved_tools:
            return False

        # Extract server/tool from full tool name (e.g. mcp__gobby-tasks__create_task)
        parts = tool_name.split("__")
        server_name = parts[1] if len(parts) >= 3 else ""
        short_tool = parts[-1] if parts else tool_name

        # Check specific policies first
        for policy in config.policies:
            if fnmatch(server_name, policy.server_pattern) and fnmatch(
                short_tool, policy.tool_pattern
            ):
                needs_approval: bool = policy.policy != "auto"
                return needs_approval

        # Fall back to default policy
        default_needs: bool = config.default_policy != "auto"
        return default_needs

    def _is_dangerous_bash(self, input_data: dict[str, Any]) -> bool:
        """Check if a Bash command matches dangerous patterns."""
        command = input_data.get("command", "")
        if not command:
            return False
        return bool(self._DANGEROUS_BASH_PATTERNS.search(command))

    def _is_write_bash(self, input_data: dict[str, Any]) -> bool:
        """Check if a Bash command performs write/destructive operations (plan mode)."""
        command = input_data.get("command", "")
        if not command:
            return False
        return bool(_BASH_WRITE_PATTERNS.search(command))

    def set_chat_mode(self, mode: str) -> None:
        """Set chat mode, resetting plan state when entering plan mode."""
        self.chat_mode = mode
        if mode == "plan":
            self._plan_approved = False
            self._plan_feedback = None
            self._plan_file_path = None
        elif mode != "plan":
            # Leaving plan mode — clear plan state
            self._plan_approved = False
            self._plan_feedback = None
        # Persist to DB (best-effort, fire-and-forget)
        if self._on_mode_persist:
            try:
                self._on_mode_persist(mode)
            except Exception as e:
                logger.warning("Failed to persist chat_mode=%s: %s", mode, e)

    def approve_plan(self) -> None:
        """Mark the current plan as approved, unlocking write tools."""
        self._plan_approved = True

    def set_plan_feedback(self, feedback: str) -> None:
        """Store user feedback for plan revision."""
        self._plan_feedback = feedback

    def provide_plan_decision(self, decision: str) -> None:
        """Provide plan approval decision, unblocking the ExitPlanMode callback.

        Args:
            decision: "approve" or "request_changes"
        """
        self._pending_plan_decision = decision
        if self._pending_plan_event is not None:
            self._pending_plan_event.set()

    @property
    def has_pending_plan(self) -> bool:
        """Whether an ExitPlanMode is currently awaiting a response."""
        return self._pending_plan_event is not None

    def _read_plan_file(self) -> str | None:
        """Read the plan file written during plan mode, if any.

        If _plan_file_path was tracked (from a Write/Edit to a plan path),
        read that file directly. Otherwise, fall back to finding the most
        recently modified .md file in .gobby/plans/ or .claude/plans/.
        """
        from pathlib import Path

        if self._plan_file_path:
            try:
                path = Path(self._plan_file_path)
                if path.exists():
                    return path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to read plan file %s: %s", self._plan_file_path, e)

        # Fallback: find the most recently modified plan file
        try:
            plan_dirs = [Path(".gobby/plans"), Path(".claude/plans")]
            # Also check home directory
            home = Path.home()
            plan_dirs.append(home / ".claude" / "plans")

            candidates: list[Path] = []
            for d in plan_dirs:
                if d.is_dir():
                    candidates.extend(d.glob("*.md"))

            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                logger.info("Plan file path not tracked; using most recent: %s", newest)
                self._plan_file_path = str(newest)
                return newest.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to find fallback plan file: %s", e)

        return None

    def _consume_plan_mode_context(self) -> str | None:
        """Return plan mode system context for additionalContext injection.

        NOTE: This method has a side effect — it clears ``_plan_feedback``
        after injecting it so the feedback is only sent once.  The name
        ``_consume_`` signals this mutation to callers.
        """
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
            "ALLOWED: Read, Glob, Grep, read-only Bash (ls, cat, grep, git status/log/diff, find), Write/Edit to ~/.claude/plans/*.md and .gobby/plans/*.md",
            "BLOCKED: Edit, Write, NotebookEdit, write/destructive Bash (rm, mv, git add/commit/push, redirects)",
            "",
            "Present a structured plan with:",
            "1. Summary of changes needed",
            "2. Files to modify and what changes to make",
            "3. Implementation order",
            "4. Verification steps",
            "",
            "When your plan is complete, you MUST call the ExitPlanMode tool to submit it for user approval.",
            "Write the plan to a .gobby/plans/*.md file first, then call ExitPlanMode.",
            "The user will see your plan and approve or request changes before you proceed.",
        ]

        if self._plan_feedback:
            parts.append("")
            parts.append(f"USER FEEDBACK on previous plan:\n{self._plan_feedback}")
            self._plan_feedback = None  # Clear after injection

        parts.append("</plan-mode>")
        return "\n".join(parts)

    async def _wait_for_tool_approval(
        self, tool_name: str, input_data: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Block until the user approves or rejects a tool call."""
        self._pending_approval = {
            "tool_name": tool_name,
            "arguments": input_data,
        }
        self._pending_approval_decision = None
        self._pending_approval_event = asyncio.Event()

        # Notify the frontend via callback (set by the websocket handler)
        if self._tool_approval_callback:
            await self._tool_approval_callback(tool_name, input_data)

        try:
            await asyncio.wait_for(self._pending_approval_event.wait(), timeout=300.0)
        except TimeoutError:
            self._pending_approval_decision = "reject"
            logger.warning(
                "Tool approval timed out",
                extra={"tool": tool_name, "conversation_id": self.conversation_id},
            )

        decision = self._pending_approval_decision or "reject"

        # Clear pending state
        self._pending_approval = None
        self._pending_approval_event = None
        self._pending_approval_decision = None

        if decision == "reject":
            return PermissionResultDeny(message=f"User rejected tool call: {tool_name}")

        if decision == "approve_always":
            self._approved_tools.add(tool_name)
            return PermissionResultAllow(updated_input=input_data)

        if decision == "approve":
            return PermissionResultAllow(updated_input=input_data)

        # Unknown decision value — treat as rejection
        return PermissionResultDeny(message=f"User rejected tool call: {tool_name}")

    def provide_approval(self, decision: str) -> None:
        """Provide approval decision for a pending tool call."""
        self._pending_approval_decision = decision
        if self._pending_approval_event is not None:
            self._pending_approval_event.set()

    @property
    def has_pending_approval(self) -> bool:
        """Whether a tool approval is currently awaiting a response."""
        return self._pending_approval is not None
