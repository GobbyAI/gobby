from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.skills.formatting import format_skill_fetch_context

logger = logging.getLogger(__name__)

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

        Intercepts Skill tool calls only when they positively resolve to a
        Gobby-owned skill, directing the agent to fetch the skill through
        gobby-skills and blocking the native tool call. Unresolved names fall
        through to the native CLI handler.
        """
        input_data = event.data
        tool_name = input_data.get("tool_name", "unknown")
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"BEFORE_TOOL: {tool_name}, session {session_id}")
        else:
            self.logger.debug(f"BEFORE_TOOL: {tool_name}")

        # Intercept Skill tool calls to resolve gobby skills
        if tool_name == "Skill" and (self._skill_manager or self._call_tool):
            try:
                skill_response = self._resolve_skill_tool_call(input_data)
                if skill_response is not None:
                    return skill_response
            except Exception:
                self.logger.error(
                    "Failed to resolve Skill tool call; allowing native handler",
                    exc_info=True,
                )
                return HookResponse(decision="allow")

        return HookResponse(decision="allow")

    def _resolve_skill_tool_call(self, input_data: dict[str, Any]) -> HookResponse | None:
        """Resolve a Gobby-owned Skill tool call.

        Tier 1: Local DB via HookSkillManager
        Tier 2: gobby-skills MCP get_skill

        Returns None for non-Gobby namespaces, missing skills, and unresolved
        bare names so the native CLI Skill handler can process them.
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
                return self._build_skill_response(skill.name, raw_skill_name, tool_input)

        # --- Tier 2: gobby-skills MCP get_skill fallback ---
        if self._call_tool:
            result = self._call_tool("gobby-skills", "get_skill", {"name": skill_name})
            if result and isinstance(result, dict) and result.get("success"):
                skill_data = result.get("skill") or result.get("result", {}).get("skill")
                if skill_data and isinstance(skill_data, dict) and skill_data.get("name"):
                    return self._build_skill_response(
                        skill_data.get("name", skill_name),
                        raw_skill_name,
                        tool_input,
                        source="MCP",
                    )

        return None

    def _build_skill_response(
        self,
        name: str,
        raw_skill_name: str,
        tool_input: dict[str, Any],
        source: str = "local",
    ) -> HookResponse:
        """Build a blocking HookResponse with an on-demand skill fetch directive."""
        context = format_skill_fetch_context(name, str(tool_input.get("args", "") or ""))

        self.logger.info(
            f"Resolved gobby skill '{name}' via {source} (requested: '{raw_skill_name}')",
        )

        return HookResponse(
            decision="block",
            reason=f"Gobby skill '{name}' resolved via {source} — fetch it with gobby-skills",
            context=context,
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

            if not is_failure and is_edit and self._session_manager:
                try:
                    # Check if file is internal .gobby file
                    file_path = (
                        tool_input.get("file_path")
                        or tool_input.get("target_file")
                        or tool_input.get("path")
                    )
                    repo_edit = (
                        self._resolve_repo_edit_paths(str(file_path), event.cwd)
                        if file_path
                        else None
                    )
                    repo_relative_path = repo_edit[1] if repo_edit else None
                    raw_internal = bool(file_path and ".gobby/" in str(file_path))
                    normalized_internal = bool(
                        repo_relative_path and Path(repo_relative_path).parts[:1] == (".gobby",)
                    )
                    is_internal = raw_internal or normalized_internal
                    in_repo_edit = not file_path or repo_edit is not None

                    if not is_internal and in_repo_edit:
                        # Track repo-relative file path in session variables
                        # (independent of task-claim gate — rules need this
                        # for per-session has_dirty_files scoping)
                        if file_path:
                            self._track_session_edited_file(session_id, str(file_path), event.cwd)

                            # Trigger incremental code index update
                            if self._code_index_trigger and repo_edit:
                                try:
                                    root_path = os.fspath(repo_edit[0])
                                    project_id = self._resolve_project_id(None, root_path)
                                    if project_id:
                                        self._code_index_trigger.notify_file_changed(
                                            file_path=repo_edit[1],
                                            project_id=project_id,
                                            root_path=root_path,
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
                            self._session_manager.mark_had_edits(session_id)
                except Exception as e:
                    # Don't fail the event if tracking fails
                    self.logger.warning(f"Failed to process file edit: {e}")

        else:
            self.logger.debug(f"AFTER_TOOL [{status}]: {tool_name}")

        return HookResponse(decision="allow")

    def _resolve_repo_edit_paths(self, file_path: str, cwd: str | None) -> tuple[Path, str] | None:
        """Return ``(repo_root, repo_relative_path)`` for an edited file."""
        target_path = Path(file_path)
        cwd_path = Path(cwd).resolve(strict=False) if cwd else None
        if target_path.is_absolute():
            resolved_target = target_path.resolve(strict=False)
            search_start = cwd_path or resolved_target.parent
        elif cwd_path is not None:
            resolved_target = (cwd_path / target_path).resolve(strict=False)
            search_start = cwd_path
        else:
            return None

        from gobby.utils.project_context import find_project_root

        repo_root = find_project_root(search_start) or cwd_path or resolved_target.parent
        repo_root = repo_root.resolve(strict=False)
        if not resolved_target.is_relative_to(repo_root):
            return None

        rel_path = os.path.normpath(os.fspath(resolved_target.relative_to(repo_root)))
        return repo_root, rel_path

    def _resolve_repo_relative_edit_path(self, file_path: str, cwd: str | None) -> str | None:
        """Return a repo-relative edit path, or ``None`` when the edit escapes the repo."""
        if not cwd and not Path(file_path).is_absolute():
            return os.path.normpath(file_path)

        repo_edit = self._resolve_repo_edit_paths(file_path, cwd)
        if repo_edit is None:
            return None
        return repo_edit[1]

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

            db = getattr(self._session_manager, "db", None)
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
