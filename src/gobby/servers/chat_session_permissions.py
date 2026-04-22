"""
Tool permission, approval, and plan mode logic for ChatSession.

Extracted as a mixin class to keep chat_session.py under the 1,000-line
guideline. ChatSession inherits from ChatSessionPermissionsMixin.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from gobby.hooks.normalization import canonicalize_shell_tool_name, is_shell_tool
from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    _PLAN_FILE_PATTERN,
    _PLAN_MODE_BLOCKED_TOOLS,
    PendingApproval,
)
from gobby.servers.tool_approvals import (
    DEFAULT_GLOBAL_APPROVAL_RULES,
    SAFE_MCP_PROXY_TOOLS,
    approval_key_for_tool,
    find_out_of_repo_write_path,
    get_global_approval_rules,
    is_safe_canvas_call,
    is_tool_auto_allowed,
    load_project_approval_rules,
    normalize_approved_tool_keys,
)
from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)


def _block_tool_name(tool_name: str, input_data: dict[str, Any]) -> str:
    """Return tool identity used in structured block logs."""
    if tool_name == "mcp__gobby__call_tool":
        server_name = str(input_data.get("server_name", ""))
        mcp_tool_name = str(input_data.get("tool_name", ""))
        if server_name and mcp_tool_name:
            return f"{server_name}:{mcp_tool_name}"
    return tool_name or "-"


def _resolve_session_lifecycle_block_reason(
    *,
    session_id: str,
    tool_name: str,
    input_data: dict[str, Any],
    reason: str | None,
) -> str:
    """Return a non-empty session-lifecycle block reason and warn on fallback use."""
    cleaned = (reason or "").strip()
    if cleaned:
        return cleaned
    logger.warning(
        "BLOCK fallback session=%s event=before_tool tool=%s "
        "source=session-lifecycle rule=pre-tool-callback "
        "detail=pre-tool lifecycle callback omitted reason",
        session_id,
        _block_tool_name(tool_name, input_data),
    )
    return (
        f"Session lifecycle blocked {tool_name} without providing a reason. "
        "Inspect the pre-tool lifecycle hook response."
    )


def _log_session_lifecycle_block(
    *,
    session_id: str,
    tool_name: str,
    input_data: dict[str, Any],
    reason: str,
) -> None:
    """Emit structured session-lifecycle block log for observability."""
    logger.info(
        "BLOCK session=%s event=before_tool tool=%s "
        "source=session-lifecycle rule=pre-tool-callback reason=%s",
        session_id,
        _block_tool_name(tool_name, input_data),
        reason,
    )


class ChatSessionPermissionsMixin:
    """Tool permission, approval, and plan mode logic for ChatSession.

    Declares attribute types expected from the concrete ChatSession dataclass."""

    # Attribute type stubs — actual fields live on the ChatSession dataclass
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
    _plan_approval_completed: bool
    _plan_file_path: str | None
    _last_plan_content: str | None
    _pending_plan_content: str | None
    _pending_plan_allowed_prompts: list[str] | None
    _pending_post_plan_mode: str | None
    _pending_plan_event: asyncio.Event | None
    _pending_plan_decision: str | None
    _on_mode_persist: Callable[[str], None] | None
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None
    project_path: str | None
    _pending_approval: PendingApproval | None
    _pending_approval_decision: str | None
    _pending_approval_event: asyncio.Event | None
    _preapproved_tool_use_ids: set[str]

    # Patterns that indicate dangerous bash commands (used by accept_edits mode)
    _DANGEROUS_BASH_PATTERNS = re.compile(
        r"(?:^|[;&|]\s*)(?:sudo|rm|chmod|chown|kill|killall|mkfs|dd|reboot|shutdown|halt|"
        r"systemctl|service|init|"
        r"mv\s+/|>\s*/|git\s+(?:push|reset\s+--hard|clean\s+-f))\b"
        r"|(?:curl|wget)\s+.*\|\s*(?:ba)?sh\b",
        re.MULTILINE,
    )
    _READ_ONLY_MCP_TOOL_PREFIXES = (
        "get_",
        "list_",
        "search_",
        "find_",
        "read_",
        "recall_",
        "blast_",
        "recommend_",
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
        tool_name = str(canonicalize_shell_tool_name(tool_name))
        tool_use_id = getattr(context, "tool_use_id", None)
        if isinstance(tool_use_id, str) and tool_use_id in self._preapproved_tool_use_ids:
            self._preapproved_tool_use_ids.discard(tool_use_id)
            return PermissionResultAllow(updated_input=input_data)

        # Agent-initiated plan mode transitions
        if tool_name == "EnterPlanMode":
            self.set_chat_mode("plan")
            if self._on_mode_changed:
                await self._on_mode_changed("plan", "agent_requested")
            return PermissionResultAllow(updated_input=input_data)
        if tool_name == "ExitPlanMode":
            plan_content = self._read_plan_file()
            if not plan_content:
                return PermissionResultDeny(
                    message=(
                        "No plan file found. Write your plan to a .gobby/plans/*.md "
                        "or .claude/plans/*.md file first, then call ExitPlanMode."
                    )
                )

            # If already approved (user clicked approve before ExitPlanMode
            # was called), skip blocking.
            if self._plan_approved:
                return PermissionResultAllow(updated_input=input_data)

            # Block until the user approves or rejects the plan in the UI.
            # The plan_pending_approval broadcast was already sent when the
            # agent wrote the plan file (PostToolUse hook → _on_plan_ready).
            # The plan_approval.py handler calls provide_plan_decision() to
            # unblock this event.
            self._pending_plan_event = asyncio.Event()
            self._pending_plan_decision = None

            try:
                await asyncio.wait_for(self._pending_plan_event.wait(), timeout=600.0)
            except TimeoutError:
                self._pending_plan_decision = "deny"
                logger.warning(
                    "Plan approval timed out, defaulting to deny",
                    extra={"conversation_id": self.conversation_id},
                )

            decision = self._pending_plan_decision or "deny"

            # Clear pending state
            self._pending_plan_event = None
            self._pending_plan_decision = None

            if decision == "approve":
                approved_mode = self._pending_post_plan_mode or self.chat_mode
                self._pending_post_plan_mode = None
                if approved_mode != self.chat_mode:
                    self.set_chat_mode(approved_mode)
                self._plan_approved = True
                self._clear_pending_plan_prompt()
                if self._on_mode_changed:
                    await self._on_mode_changed(self.chat_mode, "plan_approved")
                return PermissionResultAllow(updated_input=input_data)
            else:
                # request_changes — deny so the agent stays in plan mode
                self._pending_post_plan_mode = None
                self._clear_pending_plan_prompt()
                if self._on_mode_changed:
                    await self._on_mode_changed("plan", "plan_changes_requested")
                feedback = self._plan_feedback or ""
                return PermissionResultDeny(
                    message=f"User requested changes to the plan. {feedback}".strip()
                )

        return await self._resolve_tool_permission(
            tool_name,
            input_data,
            invoke_pre_tool_callback=True,
        )

    async def _resolve_tool_permission(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        *,
        invoke_pre_tool_callback: bool,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Apply standard tool rules, approvals, and question handling."""

        # Plan mode: always block normal repo mutations.
        if self.chat_mode == "plan":
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
            if is_shell_tool(tool_name) and self._is_write_bash(input_data):
                return PermissionResultDeny(
                    message=(
                        "Plan mode is active — write/destructive shell commands are blocked. "
                        "Present your plan to the user for approval before making changes."
                    )
                )

        # Fire session-lifecycle evaluation for tool blocking.
        # In the SDK path, the PreToolUse hook's permissionDecision="deny" is not
        # respected because can_use_tool already granted permission. By checking
        # here, we block tools at the SDK permission gate itself.
        if invoke_pre_tool_callback and self._on_pre_tool:
            resp = await self._on_pre_tool({"tool_name": tool_name, "tool_input": input_data})
            if resp and resp.get("decision") == "block":
                session_id = str(getattr(self, "db_session_id", None) or self.conversation_id)
                reason = _resolve_session_lifecycle_block_reason(
                    session_id=session_id,
                    tool_name=tool_name,
                    input_data=input_data,
                    reason=resp.get("reason"),
                )
                _log_session_lifecycle_block(
                    session_id=session_id,
                    tool_name=tool_name,
                    input_data=input_data,
                    reason=reason,
                )
                return PermissionResultDeny(message=reason)

        out_of_repo_path = find_out_of_repo_write_path(
            tool_name,
            input_data,
            project_path=self.project_path,
        )
        if out_of_repo_path:
            return PermissionResultDeny(
                message=(
                    "Writing outside the active repo is blocked in web chat. "
                    f"Blocked path: {out_of_repo_path}"
                )
            )

        if tool_name in SAFE_MCP_PROXY_TOOLS:
            return PermissionResultAllow(updated_input=input_data)
        if tool_name == "mcp__gobby__call_tool" and self._is_safe_canvas_call(input_data):
            return PermissionResultAllow(updated_input=input_data)

        # Check tool approval (before AskUserQuestion, which has its own flow)
        if tool_name != "AskUserQuestion":
            if self._needs_tool_approval(tool_name, input_data):
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
            logger.warning(f"AskUserQuestion timed out for session {self.conversation_id}")

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

    def _ensure_normalized_approved_tools(self) -> set[str]:
        normalized = normalize_approved_tool_keys(self._approved_tools)
        if normalized != self._approved_tools:
            self._approved_tools = normalized
            if self._on_approved_tools_persist:
                self._on_approved_tools_persist(self._approved_tools)
        return normalized

    def _global_approval_rules(self) -> list[str]:
        session_manager = getattr(self, "_session_manager_ref", None)
        db = getattr(session_manager, "db", None) if session_manager else None
        if db is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(ConfigStore(db))

    def _needs_tool_approval(self, tool_name: str, input_data: dict[str, Any]) -> bool:
        """Check if a tool requires user approval based on chat mode and config.

        Mode logic:
        - bypass: Never prompt
        - plan: Never prompt (plan-mode blocking happens earlier)
        - normal / accept_edits: prompt unless a shared allowlist matches
        """
        tool_name = str(canonicalize_shell_tool_name(tool_name))
        mode = self.chat_mode

        if mode in {"bypass", "plan"}:
            return False

        project_rules = load_project_approval_rules(self.project_path)
        global_rules = self._global_approval_rules()
        session_rules = self._ensure_normalized_approved_tools()
        return not is_tool_auto_allowed(
            tool_name,
            input_data,
            session_rules=session_rules,
            project_rules=project_rules,
            global_rules=global_rules,
        )

    def _is_dangerous_bash(self, input_data: dict[str, Any]) -> bool:
        """Check if a Bash command matches dangerous patterns."""
        command = input_data.get("command", "")
        if not command:
            return False
        return bool(self._DANGEROUS_BASH_PATTERNS.search(command))

    @staticmethod
    def _mcp_call_tool_key(input_data: dict[str, Any]) -> str:
        """Build a composite key for an MCP call_tool invocation.

        Returns the shared `mcp:<server>:<tool>` approval identity.
        """
        return approval_key_for_tool("mcp__gobby__call_tool", input_data)

    def _is_write_mcp_call(self, input_data: dict[str, Any]) -> bool:
        """Check if an MCP call_tool invocation targets a write operation."""
        if self._is_safe_canvas_call(input_data):
            return False
        tool_name = input_data.get("tool_name", "")
        if not tool_name:
            return True
        if any(tool_name.startswith(prefix) for prefix in self._READ_ONLY_MCP_TOOL_PREFIXES):
            return False
        return True

    def _is_safe_canvas_call(self, input_data: dict[str, Any]) -> bool:
        """Return True for UI-only canvas MCP calls that are safe to auto-approve."""
        return is_safe_canvas_call(input_data)

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
            self._last_plan_content = None
            self._pending_plan_content = None
            self._pending_plan_allowed_prompts = None
            self._pending_post_plan_mode = None
        elif mode != "plan":
            # Leaving plan mode — clear plan state
            self._plan_approved = False
            self._plan_feedback = None
            self._pending_plan_content = None
            self._pending_plan_allowed_prompts = None
            self._pending_post_plan_mode = None
        # Persist to DB (best-effort, fire-and-forget)
        if self._on_mode_persist:
            try:
                self._on_mode_persist(mode)
            except Exception as e:
                logger.warning(f"Failed to persist chat_mode={mode}: {e}")

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

    def _clear_pending_plan_prompt(self) -> None:
        """Clear the in-flight plan approval prompt shown in the UI."""
        self._pending_plan_content = None
        self._pending_plan_allowed_prompts = None

    def _remember_plan_artifact(
        self,
        *,
        file_path: str | None,
        content: str | None,
        allowed_prompts: list[str] | None = None,
    ) -> None:
        """Persist the active plan artifact for the current plan cycle."""
        if file_path:
            self._plan_file_path = file_path
        if content:
            self._last_plan_content = content
            self._pending_plan_content = content
        if allowed_prompts is not None:
            self._pending_plan_allowed_prompts = allowed_prompts

    def _read_plan_file(self) -> str | None:
        """Read the plan file written during plan mode, if any.

        If _plan_file_path was tracked (from a Write/Edit to a plan path),
        read that file directly. Otherwise, fall back to finding the most
        recently modified .md file in .gobby/plans/ or .claude/plans/.

        Relative paths are resolved against ``project_path`` (the CLI
        subprocess CWD) rather than the daemon's CWD, which may differ.
        """
        from pathlib import Path

        # Resolve a possibly-relative path against the project directory.
        # The CLI subprocess writes files relative to project_path, but this
        # code runs in the daemon process whose CWD may be different.
        project_root = Path(self.project_path) if self.project_path else None

        def _resolve(p: Path) -> Path:
            if not p.is_absolute() and project_root is not None:
                return project_root / p
            return p

        if self._plan_file_path:
            try:
                path = _resolve(Path(self._plan_file_path))
                if path.exists():
                    content = path.read_text(encoding="utf-8")
                    self._last_plan_content = content
                    return content
            except Exception as e:
                logger.warning(f"Failed to read plan file {self._plan_file_path}: {e}")

        if self._last_plan_content:
            return self._last_plan_content

        # Fallback: find the most recently modified plan file
        try:
            plan_dirs = [Path(".gobby/plans"), Path(".claude/plans")]
            # Resolve project-relative dirs against project_root
            plan_dirs = [_resolve(d) for d in plan_dirs]

            home = Path.home()
            for cli in (".claude", ".gemini", ".qwen", ".codex"):
                plan_dirs.append(home / cli / "plans")
            # Gemini also uses ~/.gemini/tmp/{hash}/plans/
            gemini_tmp = home / ".gemini" / "tmp"
            if gemini_tmp.is_dir():
                try:
                    for sub in gemini_tmp.iterdir():
                        plans = sub / "plans"
                        if plans.is_dir():
                            plan_dirs.append(plans)
                except (PermissionError, OSError) as e:
                    logger.warning(f"Could not scan {gemini_tmp}: {e}")

            candidates: list[Path] = []
            for d in plan_dirs:
                if d.is_dir():
                    candidates.extend(d.glob("*.md"))

            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                logger.info(f"Plan file path not tracked; using most recent: {newest}")
                self._plan_file_path = str(newest)
                content = newest.read_text(encoding="utf-8")
                self._last_plan_content = content
                return content
        except Exception as e:
            logger.warning(f"Failed to find fallback plan file: {e}")

        return None

    def _consume_plan_mode_context(self) -> str | None:
        """Return mode context for additionalContext injection.

        Returns context for ALL modes so the agent always knows its current
        mode. Critical for Plan → Act/Auto transitions: without explicit
        counter-context, the agent retains stale plan mode instructions
        from earlier in the conversation.

        NOTE: This method has a side effect — it clears ``_plan_feedback``
        after injecting it so the feedback is only sent once.  The name
        ``_consume_`` signals this mutation to callers.
        """
        if self.chat_mode != "plan":
            # Non-plan modes: SDK set_permission_mode handles mode signaling
            # to the agent via the control protocol — no context injection needed.
            return None

        if self._plan_approved:
            return (
                '<plan-mode status="approved">\n'
                "The user has approved your plan, but you are still in PLAN MODE.\n"
                "Do not execute changes until the session is explicitly switched to Act or Auto.\n"
                "</plan-mode>"
            )

        parts = [
            '<plan-mode status="active">',
            "You are in PLAN MODE. Your role is to research and design, not execute.",
            "",
            "ALLOWED: Read, Glob, Grep, read-only Bash (ls, cat, grep, git status/log/diff, find), Write/Edit to .md files under CLI config dirs (.gobby/, .claude/, .gemini/, .qwen/, .codex/)",
            "BLOCKED: Edit, Write, NotebookEdit, write/destructive Bash (rm, mv, git add/commit/push, redirects)",
            "",
            "Present a structured plan with:",
            "1. Summary of changes needed",
            "2. Files to modify and what changes to make",
            "3. Implementation order",
            "4. Verification steps",
            "",
            "When your plan is complete, write it to a .gobby/plans/<name>.md file.",
            "Then call ExitPlanMode to submit it for user approval. ExitPlanMode will block until the user approves or requests changes.",
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
            key = approval_key_for_tool(tool_name, input_data)
            self._approved_tools.add(key)
            if self._on_approved_tools_persist:
                self._on_approved_tools_persist(self._approved_tools)
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
