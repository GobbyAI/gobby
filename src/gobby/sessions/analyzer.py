"""
Transcript analyzer for autonomous session handoff.

Extracts structured context from session transcripts to support
autonomous continuity without relying on manual /clear boundaries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gobby.hooks.normalization import is_shell_tool
from gobby.sessions.transcripts.base import TranscriptParser
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.tool_activity import canonical_tool_name, commit_outcome

logger = logging.getLogger(__name__)


@dataclass
class HandoffContext:
    """Structured context for autonomous handoff."""

    active_gobby_task: dict[str, Any] | None = None
    task_progress: list[dict[str, Any]] = field(default_factory=list)
    """Chronological list of task state changes observed in transcript.
    Each entry: {"id": str, "action": str, "title": str}"""
    files_modified: list[str] = field(default_factory=list)
    git_commits: list[dict[str, Any]] = field(default_factory=list)
    git_status: str = ""
    initial_goal: str = ""
    recent_activity: list[str] = field(default_factory=list)
    key_decisions: list[str] | None = None
    unresolved_errors: list[dict[str, Any]] = field(default_factory=list)
    active_worktree: dict[str, Any] | None = None
    """Worktree context if session is operating in a worktree."""
    # Note: active_skills field removed - redundant with _build_skill_injection_context()
    # which already handles skill restoration on session start


class TranscriptAnalyzer:
    """
    Transcript analysis for handoff context.

    Primary: Claude Code
    Extensible: Other CLIs via TranscriptParser protocol
    """

    def __init__(self, parser: TranscriptParser | None = None):
        """
        Initialize TranscriptAnalyzer.

        Args:
            parser: Optional specific parser. Defaults to ClaudeTranscriptParser.
        """
        self.parser = parser or ClaudeTranscriptParser()

    # ------------------------------------------------------------------
    # Format-agnostic helpers
    # ------------------------------------------------------------------
    # Claude turns:  {"type": "user"|"assistant", "message": {"content": [blocks]}}
    # Typed-JSON turns:  {"type": "user"|"model", "content": str|[{"text":...}],
    #                     "toolCalls": [{name, args, ...}]}
    # These helpers let extract_handoff_context work with either format.

    @staticmethod
    def _get_user_text(turn: dict[str, Any]) -> str:
        """Extract the user's visible text from a supported transcript turn."""
        msg = turn.get("message")
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                return " ".join(parts).strip()
            if isinstance(content, str) and content:
                return content.strip()
            if content not in (None, ""):
                return str(content).strip()
            return " ".join(
                block["text"]
                for block in TranscriptAnalyzer._iter_content_blocks(turn)
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ).strip()
        return ""

    @staticmethod
    def _iter_content_blocks(turn: dict[str, Any]) -> list[dict[str, Any]]:
        """Return normalized content blocks from a Claude or Qwen turn.

        Every returned block has at least a ``type`` key (``"text"``,
        ``"tool_use"``, ``"tool_result"``).
        """
        msg = turn.get("message")
        if not isinstance(msg, dict):
            return []

        content = msg.get("content")
        if isinstance(content, list):
            return [block for block in content if isinstance(block, dict)]

        blocks: list[dict[str, Any]] = []
        parts = msg.get("parts", [])
        if not isinstance(parts, list):
            return blocks
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text and part.get("thought") is not True:
                blocks.append({"type": "text", "text": text})
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                block: dict[str, Any] = {
                    "type": "tool_use",
                    "name": function_call.get("name", "unknown"),
                    "input": function_call.get("args", {}),
                }
                if function_call.get("id"):
                    block["id"] = function_call["id"]
                blocks.append(block)
        return blocks

    @staticmethod
    def _is_user_turn(turn: dict[str, Any]) -> bool:
        """Return True if the turn is from the user (works for all CLI formats)."""
        return (
            turn.get("type") == "user"
            and turn.get("isMeta") is not True
            and turn.get("isCompactSummary") is not True
        )

    # ------------------------------------------------------------------

    def extract_handoff_context(
        self,
        turns: list[dict[str, Any]],
        max_turns: int | None = None,
        initial_goal: str | None = None,
    ) -> HandoffContext:
        """
        Extract context for autonomous handoff.

        Analyzes all turns to find:
        - Active task state from gobby-tasks calls
        - Files modified from Edit/Write/Bash calls
        - Git commits from Bash calls
        - The original user goal (first user message)
        - Recent tool activity summaries

        Handles Claude and current Qwen JSONL envelope formats
        transparently via ``_iter_content_blocks`` / ``_get_user_text``.

        Args:
            turns: List of transcript turns (dicts) in any supported format.
            max_turns: Deprecated, ignored. All turns are processed.

        Returns:
            HandoffContext object populated with extracted data
        """
        _ = max_turns  # Deprecated — all turns are now processed
        context = HandoffContext(initial_goal=initial_goal or "")

        if not turns:
            return context

        # 1. Extract Initial Goal (First User Message)
        if initial_goal is None:
            for turn in turns:
                if self._is_user_turn(turn):
                    context.initial_goal = self._get_user_text(turn)
                    break

        # 2. Analyze Recent Activity (Scan all turns)
        found_active_task = False
        modified_files_set: set[str] = set()
        tool_results: dict[str, tuple[str, bool]] = {}
        for turn in turns:
            for block in self._iter_content_blocks(turn):
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    tool_results[tool_use_id] = (
                        self._tool_result_text(block.get("content")),
                        bool(block.get("is_error")),
                    )

        for turn in reversed(turns):
            for block in self._iter_content_blocks(turn):
                if block.get("type") == "tool_use":
                    canonical_block = self._canonical_tool_block(block)
                    tool_use_id = canonical_block.get("id")
                    result = tool_results.get(tool_use_id) if isinstance(tool_use_id, str) else None
                    if (
                        isinstance(canonical_block.get("name"), str)
                        and canonical_block["name"].startswith("mcp gobby-tasks:")
                        and result is not None
                        and result[1]
                    ):
                        continue
                    self._analyze_tool_use(
                        canonical_block,
                        context,
                        found_active_task,
                        modified_files_set,
                        result,
                    )

        context.files_modified = sorted(modified_files_set)
        # task_progress was built in reverse order; restore chronological
        context.task_progress.reverse()

        # 3. Recent Activity Summary (Last 10 calls)
        recent_tools: list[str] = []
        count = 0
        for turn in reversed(turns):
            if count >= 10:
                break
            for block in self._iter_content_blocks(turn):
                if block.get("type") == "tool_use":
                    canonical_block = self._canonical_tool_block(block)
                    description = self._format_tool_description(canonical_block)
                    canonical_name = canonical_block.get("name")
                    if isinstance(canonical_name, str) and canonical_name.startswith("mcp "):
                        description = f"{canonical_name}: {description}"
                    recent_tools.append(description)
                    count += 1
                    if count >= 10:
                        break
        context.recent_activity = recent_tools

        # 4. Extract Key Decisions from assistant text blocks
        decision_indicators = [
            "decided",
            "approach",
            "instead",
            "because",
            "chosen",
            "opted for",
            "went with",
            "switching to",
            "rather than",
        ]
        decisions: list[str] = []
        for turn in turns:
            for block in self._iter_content_blocks(turn):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    text_lower = text.lower()
                    if any(ind in text_lower for ind in decision_indicators):
                        snippet = text[:200].strip()
                        if snippet:
                            decisions.append(snippet)
                            if len(decisions) >= 10:
                                break
            if len(decisions) >= 10:
                break
        if decisions:
            context.key_decisions = decisions

        return context

    @staticmethod
    def _canonical_tool_block(block: dict[str, Any]) -> dict[str, Any]:
        raw_name = block.get("name")
        raw_input = block.get("input")
        if not isinstance(raw_name, str):
            return {**block, "name": "unknown", "input": {}}
        name, tool_input = canonical_tool_name(raw_name, raw_input)
        if name == raw_name and raw_name in {"mcp_call_tool", "mcp__gobby__call_tool"}:
            tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
        return {**block, "name": name, "input": tool_input}

    @staticmethod
    def _tool_result_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(TranscriptAnalyzer._tool_result_text(item) for item in value)
        if isinstance(value, dict):
            for key in ("text", "output", "content", "message", "error"):
                if key in value:
                    text = TranscriptAnalyzer._tool_result_text(value[key])
                    if text:
                        return text
        return ""

    def _analyze_tool_use(
        self,
        block: dict[str, Any],
        context: HandoffContext,
        found_active_task: bool,
        modified_files_set: set[str],
        result: tuple[str, bool] | None = None,
    ) -> None:
        """Helper to analyze a single tool use block."""
        block = self._canonical_tool_block(block)
        tool_name = block.get("name")
        tool_input = block.get("input", {})

        # -- Gobby Tasks --
        if isinstance(tool_name, str) and tool_name.startswith("mcp "):
            server_tool = tool_name.removeprefix("mcp ")
            server, separator, tool = server_tool.partition(":")
            args = tool_input

            if separator and server == "gobby-tasks":
                # Track all task interactions for task_progress
                task_id = args.get("task_id") or args.get("id")
                title = args.get("title", "")
                if task_id and tool:
                    context.task_progress.append(
                        {
                            "id": task_id,
                            "action": tool,
                            "title": title or f"Task {task_id}",
                        }
                    )

                # We want the most recent task interaction that implies working on a task
                # e.g., create_task, update_task, get_task
                if not context.active_gobby_task:
                    if task_id:
                        context.active_gobby_task = {
                            "id": task_id,
                            "action": tool,
                            "title": args.get("title", f"Task {task_id}"),
                        }

                commit_sha = args.get("commit_sha")
                if tool in {"close_task", "link_commit"} and isinstance(commit_sha, str):
                    context.git_commits.append(
                        {
                            "hash": commit_sha,
                            "message": f"{tool} {task_id or ''}".strip(),
                        }
                    )

        # -- File Modifications --
        elif tool_name in {
            "Edit",
            "Write",
            "Replace",
            "apply_patch",
            "create_file",
            "edit_file",
            "replace_file_content",
            "search_replace",
            "write",
            "write_file",
            "write_to_file",
        }:
            # Claude Code uses Edit/Write; Codex uses write_to_file/replace_file_content
            # Support both standard and generic names.
            path = (
                tool_input.get("file_path")
                or tool_input.get("TargetFile")
                or tool_input.get("path")
                or tool_input.get("target_file")
            )
            if path:
                modified_files_set.add(path)

        # -- Git Commits --
        elif isinstance(tool_name, str) and is_shell_tool(tool_name):
            command = tool_input.get("command", "")
            if "git commit" in command:
                commit_hash = ""
                message = command
                if result is not None and not result[1]:
                    outcome = (
                        result[0]
                        if result[0].startswith("commit ")
                        else commit_outcome(tool_name, tool_input, result[0])
                    )
                    if outcome:
                        parts = outcome.split(" ", 2)
                        if len(parts) >= 2:
                            commit_hash = parts[1]
                        if len(parts) == 3:
                            message = parts[2]
                commit = {"hash": commit_hash, "message": message}
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str):
                    commit["tool_use_id"] = tool_use_id
                context.git_commits.append(commit)

    def _format_tool_description(self, block: dict[str, Any]) -> str:
        """
        Format a tool use block into a human-readable description.

        Extracts meaningful details instead of just showing the tool name.

        Args:
            block: Tool use block with 'name' and 'input' keys

        Returns:
            Human-readable description of what the tool call did
        """
        block = self._canonical_tool_block(block)
        tool_name = block.get("name", "unknown")
        tool_input = block.get("input", {})

        # MCP tool calls - show server.tool with details for gobby-tasks
        if isinstance(tool_name, str) and tool_name.startswith("mcp "):
            server_tool = tool_name.removeprefix("mcp ")
            server, separator, tool = server_tool.partition(":")
            if not separator:
                server, tool = "unknown", "unknown"
            args = tool_input

            # Enhanced formatting for gobby-tasks operations
            if server == "gobby-tasks":
                if tool == "create_task":
                    title = args.get("title", "Untitled")
                    parent = args.get("parent_task_id", "")
                    if parent:
                        return f"Created task: {title} (parent: {parent})"
                    return f"Created task: {title}"
                elif tool == "update_task":
                    task_id = args.get("task_id", "?")
                    return f"Updated task {task_id}"
                elif tool == "close_task":
                    task_id = args.get("task_id", "?")
                    if args.get("preview") is True:
                        return f"Conditionally closed task {task_id}"
                    reason = args.get("reason", "")
                    if reason:
                        # Truncate long reasons
                        if len(reason) > 40:
                            reason = reason[:37] + "..."
                        return f"Closed task {task_id}: {reason}"
                    return f"Closed task {task_id}"
                elif tool == "claim_task":
                    task_id = args.get("task_id", "?")
                    return f"Claimed task {task_id}"
                elif tool == "get_task":
                    task_id = args.get("task_id", "?")
                    return f"Fetched task {task_id}"

            # Generic MCP call formatting - extract meaningful context from args
            context = self._extract_mcp_context(args)
            if context:
                return f"{server}.{tool}: {context}"
            return f"Called {server}.{tool}"

        if tool_name in ("mcp__gobby__call_tool", "mcp_call_tool"):
            server = tool_input.get("server_name", "unknown")
            tool = tool_input.get("tool_name", "unknown")
            args = tool_input.get("arguments", {})
            if not isinstance(args, dict):
                args = {}
            context = self._extract_mcp_context(args)
            if context:
                return f"{server}.{tool}: {context}"
            return f"Called {server}.{tool}"

        # Shell tools - show the command (truncated)
        if is_shell_tool(tool_name):
            command = tool_input.get("command", "")
            # Truncate long commands
            if len(command) > 60:
                command = command[:57] + "..."
            return f"Ran: {command}"

        # Edit/Write - show the file path
        if tool_name in ("Edit", "Write"):
            path = tool_input.get("file_path", "")
            if path:
                return f"{tool_name}: {path}"
            return f"Called {tool_name}"

        # Read - show the file path
        if tool_name == "Read":
            path = tool_input.get("file_path", "")
            if path:
                return f"Read: {path}"
            return "Called Read"

        # Glob - show the pattern
        if tool_name == "Glob":
            pattern = tool_input.get("pattern", "")
            if pattern:
                return f"Glob: {pattern}"
            return "Called Glob"

        # Grep - show the pattern
        if tool_name == "Grep":
            pattern = tool_input.get("pattern", "")
            if pattern:
                # Truncate long patterns
                if len(pattern) > 40:
                    pattern = pattern[:37] + "..."
                return f"Grep: {pattern}"
            return "Called Grep"

        # Task tool - show subagent type
        if tool_name == "Task":
            subagent = tool_input.get("subagent_type", "")
            desc = tool_input.get("description", "")
            if subagent:
                return f"Task ({subagent}): {desc}" if desc else f"Task ({subagent})"
            return f"Task: {desc}" if desc else "Called Task"

        # Default - just show the tool name
        return f"Called {tool_name}"

    def _extract_mcp_context(self, args: dict[str, Any]) -> str:
        """
        Extract meaningful context from MCP tool arguments.

        Looks for common argument patterns and returns the most relevant value
        to describe what the tool call is doing.

        Args:
            args: Tool arguments dict

        Returns:
            Extracted context string (truncated to 100 chars) or empty string
        """
        if not args:
            return ""

        # Priority order for extracting context
        # 1. Search/query related - what are we looking for?
        for key in ("query", "search", "pattern", "topic"):
            if key in args and args[key]:
                return self._truncate(str(args[key]), 100)

        # 2. Identity/naming - what entity are we working with?
        for key in ("title", "name", "task_id", "id", "ref"):
            if key in args and args[key]:
                return self._truncate(str(args[key]), 100)

        # 3. Resource paths - what file/resource?
        for key in ("path", "file_path", "uri", "url", "file"):
            if key in args and args[key]:
                return self._truncate(str(args[key]), 100)

        # 4. Descriptive content - why/what?
        for key in ("description", "reason", "message", "content"):
            if key in args and args[key]:
                return self._truncate(str(args[key]), 100)

        # 5. Fallback: first non-empty string value
        for key, value in args.items():
            if isinstance(value, str) and value and key not in ("session_id", "server_name"):
                return self._truncate(value, 100)

        return ""

    def _truncate(self, text: str, max_len: int) -> str:
        """Truncate text to max_len, adding ellipsis if needed."""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."
