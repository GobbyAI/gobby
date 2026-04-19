from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.normalization import is_shell_tool

logger = logging.getLogger(__name__)

_GOBBY_TASKS_CLI_RE = re.compile(r"\b(?:uv\s+run\s+)?gobby\s+tasks\b")
_GOBBY_TASKS_CLI_REASON = (
    "The `gobby tasks` CLI is for humans. Use the gobby-tasks MCP server instead:\n"
    "  - list_mcp_servers() -> confirm gobby-tasks is available\n"
    "  - list_tools(server_name='gobby-tasks') -> see task tools\n"
    "  - get_tool_schema(server_name='gobby-tasks', tool_name='<tool>') -> fetch params\n"
    "  - call_tool(server_name='gobby-tasks', tool_name='<tool>', args={...})\n"
    "Common equivalents: create_task, claim_task, update_task, close_task,\n"
    "list_ready_tasks, get_task, list_tasks."
)

EDIT_TOOLS = {
    "write_file",
    "replace",
    "edit_file",
    "notebook_edit",
    "edit",
    "write",
}


class ToolEventHandlerMixin(EventHandlersBase):
    """Mixin for handling tool-related events."""

    def handle_before_tool(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_TOOL event.

        Intercepts Skill tool calls and resolves gobby skills via a 4-tier
        fallback chain, injecting skill content as context and blocking the
        native tool call (which would fail with "Unknown skill").
        """
        input_data = event.data
        tool_name = input_data.get("tool_name", "unknown")
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"BEFORE_TOOL: {tool_name}, session {session_id}")
        else:
            self.logger.debug(f"BEFORE_TOOL: {tool_name}")

        block_response = self._maybe_block_gobby_tasks_cli(tool_name, input_data)
        if block_response is not None:
            return block_response

        # Intercept Skill tool calls to resolve gobby skills
        if tool_name == "Skill" and (self._skill_manager or self._call_tool):
            try:
                skill_response = self._resolve_skill_tool_call(input_data)
                if skill_response is not None:
                    return skill_response
            except Exception as e:
                self.logger.error(f"Failed to resolve skill tool call: {e}", exc_info=True)
                skill_name = input_data.get("tool_input", {}).get("skill", "unknown")
                return HookResponse(
                    decision="block",
                    reason="Skill resolution failed",
                    context=(
                        f"An error occurred while resolving skill '{skill_name}'. "
                        "Please try again or search for skills with: "
                        f"call_tool('gobby-skills', 'search_skills', {{'query': '{skill_name}'}})"
                    ),
                )

        return HookResponse(decision="allow")

    def _maybe_block_gobby_tasks_cli(
        self, tool_name: str, input_data: dict[str, Any]
    ) -> HookResponse | None:
        """Block native shell access to ``gobby tasks`` across adapters."""
        if not is_shell_tool(tool_name):
            return None

        tool_input = input_data.get("tool_input", {})
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command", "") or "")
        elif isinstance(tool_input, str):
            command = tool_input

        if not command or not _GOBBY_TASKS_CLI_RE.search(command):
            return None

        return HookResponse(
            decision="block",
            reason=_GOBBY_TASKS_CLI_REASON,
            context=_GOBBY_TASKS_CLI_REASON,
        )

    def _resolve_skill_tool_call(self, input_data: dict[str, Any]) -> HookResponse | None:
        """Resolve a Skill tool call via 4-tier fallback chain.

        Tier 1: Local DB via HookSkillManager
        Tier 2: gobby-skills MCP get_skill
        Tier 3: Hub search with install nudge
        Tier 4: Block with helpful error

        Never falls through to Claude Code's native Skill handler.
        Returns None only for non-gobby namespaced skills.
        """
        tool_input = input_data.get("tool_input", {})
        raw_skill_name = tool_input.get("skill", "")
        if not raw_skill_name:
            return None

        # Strip gobby: namespace prefix (Skill tool namespace separator)
        skill_name = raw_skill_name
        if skill_name.startswith("gobby:"):
            skill_name = skill_name[len("gobby:") :]

        # Non-gobby namespace (e.g. "ms-office-suite:pdf") — not ours
        if ":" in skill_name:
            return None

        # --- Tier 1: Local DB resolve ---
        if self._skill_manager:
            skill = self._skill_manager.resolve_skill_name(skill_name)
            if skill is not None:
                return self._build_skill_response(
                    skill.name, skill.content, raw_skill_name, tool_input
                )

        # --- Tier 2: gobby-skills MCP get_skill fallback ---
        if self._call_tool:
            result = self._call_tool("gobby-skills", "get_skill", {"name": skill_name})
            if result and isinstance(result, dict) and result.get("success"):
                skill_data = result.get("skill") or result.get("result", {}).get("skill")
                if skill_data and isinstance(skill_data, dict) and skill_data.get("content"):
                    return self._build_skill_response(
                        skill_data.get("name", skill_name),
                        skill_data["content"],
                        raw_skill_name,
                        tool_input,
                        source="MCP",
                    )

        # --- Tier 3: Hub search nudge ---
        if self._call_tool:
            result = self._call_tool(
                "gobby-skills", "search_hub", {"query": skill_name, "limit": 5}
            )
            if result and isinstance(result, dict) and result.get("success"):
                hub_results = result.get("results") or result.get("result", {}).get("results", [])
                if hub_results:
                    return self._build_hub_nudge_response(skill_name, hub_results)

        # --- Tier 4: Nothing found — block with clear error ---
        return HookResponse(
            decision="block",
            reason=f"Skill '{skill_name}' not found in local DB or skill hubs",
            context=(
                f"The skill '{skill_name}' was not found locally or in any skill hubs.\n\n"
                f"Search for installed skills:\n"
                f"  call_tool('gobby-skills', 'search_skills', {{'query': '{skill_name}'}})\n\n"
                f"Search skill hubs for installable skills:\n"
                f"  call_tool('gobby-skills', 'search_hub', {{'query': '{skill_name}'}})"
            ),
        )

    def _build_skill_response(
        self,
        name: str,
        content: str,
        raw_skill_name: str,
        tool_input: dict[str, Any],
        source: str = "local",
    ) -> HookResponse:
        """Build a blocking HookResponse with skill content injected as context."""
        parts = [f'<skill-context name="{name}">']
        parts.append(content)
        parts.append("</skill-context>")

        args = tool_input.get("args", "")
        if args:
            parts.append(f"\nUser arguments: {args}")

        context = "\n".join(parts)

        self.logger.info(
            f"Resolved gobby skill '{name}' via {source} (requested: '{raw_skill_name}')",
        )

        return HookResponse(
            decision="block",
            reason=f"Gobby skill '{name}' resolved via {source} — content injected as context",
            context=context,
        )

    def _build_hub_nudge_response(
        self, skill_name: str, hub_results: list[dict[str, Any]]
    ) -> HookResponse:
        """Build a blocking HookResponse with hub search results and install instructions."""
        lines = [f"Found matching skills in skill hubs for '{skill_name}':\n"]
        for r in hub_results[:5]:
            name = r.get("display_name") or r.get("name") or r.get("slug", "unknown")
            desc = r.get("description", "")
            hub = r.get("hub_name") or r.get("hub", "")
            slug = r.get("slug") or r.get("name", name)
            source_ref = f"{hub}:{slug}" if hub else slug
            lines.append(f"- **{name}**: {desc}")
            lines.append(
                f"  Install: call_tool('gobby-skills', 'install_skill', "
                f"{{'source': '{source_ref}'}})"
            )
        lines.append(f'\nAfter installing, retry: Skill("{skill_name}")')

        self.logger.info(
            f"Skill '{skill_name}' not installed - {len(hub_results)} hub matches found",
        )

        return HookResponse(
            decision="block",
            reason=f"Skill '{skill_name}' not installed - hub matches found",
            context="\n".join(lines),
        )

    def handle_after_tool(self, event: HookEvent) -> HookResponse:
        """Handle AFTER_TOOL event."""
        input_data = event.data
        tool_name = input_data.get("tool_name", "unknown")
        session_id = event.metadata.get("_platform_session_id")
        is_failure = event.metadata.get("is_failure", False)

        status = "FAIL" if is_failure else "OK"
        if session_id:
            self.logger.debug(f"AFTER_TOOL [{status}]: {tool_name}, session {session_id}")

            # Track edits for session high-water mark
            # Only if tool succeeded, matches edit tools, and session has claimed a task
            # Skip .gobby/ internal files (tasks.jsonl, memories.jsonl, etc.)
            tool_input = input_data.get("tool_input", {})

            # Simple check for edit tools (case-insensitive)
            is_edit = tool_name.lower() in EDIT_TOOLS

            # For complex tools (multi_replace, etc), check if they modify files
            # This logic could be expanded, but for now stick to the basic set

            if not is_failure and is_edit and self._session_storage:
                try:
                    # Check if file is internal .gobby file
                    file_path = (
                        tool_input.get("file_path")
                        or tool_input.get("target_file")
                        or tool_input.get("path")
                    )
                    is_internal = file_path and ".gobby/" in str(file_path)
                    repo_relative_path = (
                        self._resolve_repo_relative_edit_path(str(file_path), event.cwd)
                        if file_path
                        else None
                    )
                    in_repo_edit = not file_path or repo_relative_path is not None

                    if not is_internal and in_repo_edit:
                        # Track repo-relative file path in session variables
                        # (independent of task-claim gate — rules need this
                        # for per-session has_dirty_files scoping)
                        if file_path:
                            self._track_session_edited_file(session_id, str(file_path), event.cwd)

                            # Trigger incremental code index update
                            if self._code_index_trigger:
                                try:
                                    project_id = self._resolve_project_id(None, event.cwd)
                                    if project_id:
                                        self._code_index_trigger.notify_file_changed(
                                            file_path=str(file_path),
                                            project_id=project_id,
                                            root_path=event.cwd or "",
                                        )
                                except Exception as e:
                                    self.logger.debug(f"Failed to trigger code index update: {e}")

                        # Check if session has any claimed tasks before marking had_edits
                        has_claimed_task = False
                        if self._task_manager:
                            try:
                                claimed_tasks = self._task_manager.list_tasks(
                                    claimed_by_session_id=session_id
                                )
                                has_claimed_task = len(claimed_tasks) > 0
                            except Exception as e:
                                self.logger.debug(
                                    f"Failed to check claimed tasks for session {session_id}: {e}"
                                )

                        if has_claimed_task:
                            self._session_storage.mark_had_edits(session_id)
                except Exception as e:
                    # Don't fail the event if tracking fails
                    self.logger.warning(f"Failed to process file edit: {e}")

        else:
            self.logger.debug(f"AFTER_TOOL [{status}]: {tool_name}")

        return HookResponse(decision="allow")

    def _resolve_repo_relative_edit_path(self, file_path: str, cwd: str | None) -> str | None:
        """Return a repo-relative edit path, or ``None`` when the edit escapes ``cwd``."""
        if not cwd:
            return os.path.normpath(file_path)

        repo_root = Path(cwd).resolve(strict=False)
        target_path = Path(file_path)
        if not target_path.is_absolute():
            target_path = repo_root / target_path

        resolved_target = target_path.resolve(strict=False)
        if not resolved_target.is_relative_to(repo_root):
            return None

        return os.path.normpath(os.fspath(resolved_target.relative_to(repo_root)))

    def _track_session_edited_file(self, session_id: str, file_path: str, cwd: str | None) -> None:
        """Record a repo-relative file path in session_edited_files variable.

        Used to scope ``has_dirty_files`` to only files this session touched,
        preventing bleed across concurrent sessions sharing a working directory.
        """
        try:
            rel_path = self._resolve_repo_relative_edit_path(file_path, cwd)
            if rel_path is None:
                return

            from gobby.workflows.state_manager import SessionVariableManager

            db = getattr(self._session_storage, "db", None)
            if db:
                SessionVariableManager(db).append_to_set_variable(
                    session_id, "session_edited_files", [rel_path]
                )
        except Exception as e:
            logger.debug(f"Failed to track session edited file: {e}")

    def handle_before_tool_selection(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_TOOL_SELECTION event (Gemini only)."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"BEFORE_TOOL_SELECTION: session {session_id}")
        else:
            self.logger.debug("BEFORE_TOOL_SELECTION")

        return HookResponse(decision="allow")
