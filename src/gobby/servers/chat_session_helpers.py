"""
Module-level helpers for chat_session.py.

Contains constants, TypedDicts, CLI discovery utilities, and SDK hook
response converters. All functions are pure/stateless with no dependency
on ChatSession.
"""

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, TypedDict, cast

from claude_agent_sdk.types import (
    PostToolUseHookSpecificOutput,
    PreToolUseHookSpecificOutput,
    SyncHookJSONOutput,
    UserPromptSubmitHookSpecificOutput,
)

from gobby.llm.sdk_utils import truncate_additional_context as _truncate

logger = logging.getLogger(__name__)

# Tools that are blocked in plan mode (write operations)
_PLAN_MODE_BLOCKED_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})

# Bash commands that perform write/destructive operations (blocked in plan mode).
# Read-only commands (ls, cat, grep, git status/log/diff, find, head, tail) pass through.
_PLAN_FILE_PATTERN = re.compile(r"^(?:.*[/\\])?\.(?:claude|gobby)[/\\]plans[/\\].*\.md$")

_BASH_WRITE_PATTERNS = re.compile(
    r"(?:^|[;&|]\s*)(?:"
    r"rm\b|mv\b|cp\b|mkdir\b|touch\b|chmod\b|chown\b"
    r"|sed\s+-i\b|pip\s+install\b|npm\s+install\b|yarn\s+add\b"
    r"|git\s+(?:add|commit|push|reset|clean|checkout|merge|rebase|cherry-pick)\b"
    r")"
    r"|(?<![0-9&])>\s*[^\s&]"  # output redirection (not 2>/dev/null, &>/dev/null)
    r"|(?<![0-9&])>>\s*[^\s]",  # append redirection (not 2>>, &>>)
    re.MULTILINE,
)


class PendingApproval(TypedDict):
    """Pending tool-approval payload sent to the frontend."""

    tool_name: str
    arguments: dict[str, Any]


# Fallback system prompt if the prompts system is unavailable
_FALLBACK_SYSTEM_PROMPT = "You are Gobby, a helpful AI coding assistant."


def _load_chat_system_prompt(db: Any = None) -> str:
    """Load the chat system prompt from the prompts system.

    Uses PromptLoader with database-backed precedence:
    project -> global -> bundled.

    Args:
        db: Database connection for prompt loading. Falls back to default if None.
    """
    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=db)
        template = loader.load("chat/system")
        return template.content
    except Exception as e:
        logger.warning("Failed to load chat/system prompt, using fallback: %s", e)
        return _FALLBACK_SYSTEM_PROMPT


def _find_cli_path() -> str | None:
    """Find Claude CLI path without resolving symlinks."""
    cli_path = shutil.which("claude")
    if cli_path and os.path.exists(cli_path) and os.access(cli_path, os.X_OK):
        return cli_path
    return None


def _find_project_root() -> Path | None:
    """Find the gobby project root from source tree.

    In dev mode the daemon runs from the repo, so we can derive the
    project root from this file's location.
    """
    candidate = Path(__file__).parent.parent.parent.parent
    if (candidate / ".gobby").is_dir():
        return candidate
    return None


def _find_mcp_config() -> str | None:
    """Find .mcp.json config file for MCP tool access."""
    cwd_config = Path.cwd() / ".mcp.json"
    if cwd_config.exists():
        return str(cwd_config)

    project_root = _find_project_root()
    if project_root:
        config = project_root / ".mcp.json"
        if config.exists():
            return str(config)

    return None


def _response_to_prompt_output(resp: dict[str, Any] | None) -> SyncHookJSONOutput:
    """Convert workflow HookResponse dict to UserPromptSubmit SDK output."""
    if not resp:
        return SyncHookJSONOutput()
    output = SyncHookJSONOutput()
    if resp.get("decision") == "block":
        output["decision"] = "block"
        if resp.get("reason"):
            output["reason"] = resp["reason"]
    context = resp.get("context")
    if context:
        output["hookSpecificOutput"] = UserPromptSubmitHookSpecificOutput(
            hookEventName="UserPromptSubmit",
            additionalContext=_truncate(context),
        )
    return output


def _response_to_pre_tool_output(resp: dict[str, Any] | None) -> SyncHookJSONOutput:
    """Convert workflow HookResponse dict to PreToolUse SDK output."""
    if not resp:
        return SyncHookJSONOutput()
    output = SyncHookJSONOutput()
    if resp.get("decision") == "block":
        specific: PreToolUseHookSpecificOutput = PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse",
            permissionDecision="deny",
        )
        if resp.get("reason"):
            specific["permissionDecisionReason"] = resp["reason"]
        output["hookSpecificOutput"] = specific
        if resp.get("reason"):
            output["reason"] = resp["reason"]
    elif resp.get("modified_input"):
        # Rewrite tool input (rewrite_input effect)
        specific_rewrite = PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse",
        )
        specific_rewrite["updatedInput"] = resp["modified_input"]
        if resp.get("auto_approve"):
            specific_rewrite["permissionDecision"] = "allow"
        if resp.get("context"):
            specific_rewrite["additionalContext"] = _truncate(resp["context"])
        output["hookSpecificOutput"] = specific_rewrite
    elif resp.get("context"):
        output["hookSpecificOutput"] = PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse",
        )
        output["hookSpecificOutput"]["additionalContext"] = _truncate(resp["context"])  # type: ignore[typeddict-unknown-key]
    return output


def _response_to_post_tool_output(resp: dict[str, Any] | None) -> SyncHookJSONOutput:
    """Convert workflow HookResponse dict to PostToolUse SDK output."""
    if not resp:
        return SyncHookJSONOutput()
    output = SyncHookJSONOutput()

    # Compressed MCP output (compress_output effect)
    modified_output = resp.get("modified_output")
    if modified_output is not None:
        hook_specific = PostToolUseHookSpecificOutput(
            hookEventName="PostToolUse",
        )
        hook_specific["updatedMCPToolOutput"] = modified_output
        context = resp.get("context")
        if context:
            hook_specific["additionalContext"] = _truncate(context)
        output["hookSpecificOutput"] = hook_specific
        return output

    context = resp.get("context")
    if context:
        output["hookSpecificOutput"] = PostToolUseHookSpecificOutput(
            hookEventName="PostToolUse",
            additionalContext=_truncate(context),
        )
    return output


def _response_to_stop_output(resp: dict[str, Any] | None) -> SyncHookJSONOutput:
    """Convert workflow HookResponse dict to Stop SDK output."""
    if not resp:
        return SyncHookJSONOutput()
    output = SyncHookJSONOutput()
    if resp.get("decision") == "block":
        output["decision"] = "block"
        if resp.get("reason"):
            output["reason"] = resp["reason"]
    context = resp.get("context")
    if context:
        output["hookSpecificOutput"] = cast(
            Any,
            {  # No SDK TypedDict for Stop
                "hookEventName": "Stop",
                "additionalContext": _truncate(context),
            },
        )
    return output


def build_compaction_context(
    *,
    session_ref: str,
    project_id: str | None = None,
    cwd: str | None = None,
    source: str | None = None,
) -> str:
    """Build essential Gobby context that must survive compaction.

    Injected as additionalContext in PreCompact hooks so the agent
    retains its identity and tool usage instructions after the SDK
    compacts the conversation.
    """
    parts = [f"Gobby Session ID: {session_ref}"]
    if project_id:
        parts.append(f"Project ID: {project_id}")
    if cwd:
        parts.append(f"Working directory: {cwd}")
    if source:
        parts.append(f"Source: {source}")
    parts.append("Use session_id in MCP tool calls (gobby-tasks, gobby-memory, etc.)")
    return "\n".join(parts)


def _response_to_compact_output(resp: dict[str, Any] | None) -> SyncHookJSONOutput:
    """Convert workflow HookResponse dict to PreCompact SDK output."""
    if not resp:
        return SyncHookJSONOutput()
    output = SyncHookJSONOutput()
    context = resp.get("context")
    if context:
        output["hookSpecificOutput"] = cast(
            Any,
            {  # No SDK TypedDict for PreCompact
                "hookEventName": "PreCompact",
                "additionalContext": _truncate(context),
            },
        )
    return output
