from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.codex_impl.execution_chain import (
    FUNCTIONS_EXEC_NAMES,
    validate_functions_exec_wrapper,
)
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.hooks._normalization_canonical import CANONICAL_WRITE_TOOL_NAMES
from gobby.hooks.dispatchers import run_coro_blocking
from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.terminal_handoff_delivery import schedule_terminal_handoff_delivery
from gobby.hooks.tool_error_tracker import is_wrapper_echo_event, track_tool_outcome
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import capability_menu, route_gobby_request, standalone_menu
from gobby.skills.parser import ParsedSkill
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    active_task_id_for_edit,
    task_edited_file_set_for_checkout,
)
from gobby.workflows.task_dirty_state import paths_committed_after_async

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

EDIT_TOOLS = CANONICAL_WRITE_TOOL_NAMES


class SkillResolutionError(RuntimeError):
    """Expected failure while resolving a Skill tool name."""


_EXPECTED_SKILL_RESOLUTION_ERRORS = (
    ConnectionError,
    # Skill lookup may touch filesystem-backed skill stores; only the expected
    # path/permission failures should fall through to the native Skill handler.
    FileNotFoundError,
    LookupError,
    PermissionError,
    TimeoutError,
)


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
        project_id = event.project_id or self._resolve_project_id(event.project_id, event.cwd)

        if session_id:
            self.logger.debug("BEFORE_TOOL: %s, session %s", tool_name, session_id)
            ensure_subagent = getattr(self, "_ensure_bound_grok_native_subagent", None)
            if ensure_subagent is not None:
                ensure_subagent(event, session_id)
            if event.wait_kind is not None:
                self._enter_turn_wait(event, event.wait_kind)
            else:
                self._resume_turn_lifecycle(event)
        else:
            self.logger.debug("BEFORE_TOOL: %s", tool_name)

        if event.source.value == "codex" and tool_name in FUNCTIONS_EXEC_NAMES:
            wrapper_error = validate_functions_exec_wrapper(
                input_data.get("arguments", input_data.get("tool_input"))
            )
            if wrapper_error is not None:
                return HookResponse(decision="block", reason=wrapper_error)

        # Intercept Skill tool calls to resolve gobby skills
        if tool_name == "Skill":
            try:
                skill_response = self._resolve_skill_tool_call(input_data, project_id, session_id)
                if skill_response is not None:
                    return skill_response
            except SkillResolutionError:
                self.logger.warning(
                    "Failed to resolve Skill tool call; allowing native handler",
                    exc_info=True,
                )
                return HookResponse(decision="allow")

        if session_id and self._progress_tracker is not None:
            try:
                self._progress_tracker.record_tool_start(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=input_data.get("tool_input", {}),
                )
            except Exception as e:
                self.logger.warning("Failed to record autonomous tool start: %s", e)

        return HookResponse(decision="allow")

    def _resolve_skill_tool_call(
        self,
        input_data: dict[str, Any],
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> HookResponse | None:
        """Resolve a Gobby-owned Skill tool call.

        Resolve installed metadata through HookSkillManager. Never fetch a body to
        test existence: a completed MCP get_skill would record an invisible load.

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

        # Capability names own only the Gobby namespace. A bare native name such as
        # "config" stays with the provider unless it resolves to a Gobby skill below.
        catalog = None
        if skill_name == "gobby" or raw_skill_name.startswith("gobby:"):
            try:
                catalog = load_capability_catalog()
            except (OSError, ValueError) as exc:
                raise SkillResolutionError("Gobby capability catalog is unavailable") from exc
        if catalog is not None and (
            skill_name == "gobby" or any(item.name == skill_name for item in catalog.capabilities)
        ):
            args = str(tool_input.get("args", "") or "")
            request = args if skill_name == "gobby" else f"{skill_name} {args}"
            manager = self._skill_manager
            route = route_gobby_request(
                request,
                resolve_skill=lambda name: (
                    manager.resolve_skill_name(name, project_id=project_id) if manager else None
                ),
                catalog=catalog,
            )
            context = route.context
            if route.kind in ("help", "unknown"):
                if route.kind == "unknown":
                    context = f"Unknown Gobby name {route.unknown_name!r}.\n\n"
                    context += capability_menu(catalog, "/gobby")
                skills = self._router_skills(session_id, project_id)
                context += "\n\nInstalled standalone skills:\n" + standalone_menu(
                    skills, catalog, "/gobby"
                )
                context += "\nMenus list choices only; do not execute listed operations."
            elif route.arguments:
                context += f"\n\nUser arguments: {route.arguments}"
            return HookResponse(
                decision="block",
                reason="Gobby router resolved through its capability catalog",
                context=context,
            )

        # --- Tier 1: Local DB resolve ---
        if self._skill_manager:
            try:
                project_kwargs = {"project_id": project_id} if project_id is not None else {}
                skill = self._skill_manager.resolve_skill_name(skill_name, **project_kwargs)
            except _EXPECTED_SKILL_RESOLUTION_ERRORS as exc:
                raise SkillResolutionError(
                    f"Local skill resolution failed for {skill_name!r}"
                ) from exc
            if skill is not None:
                return self._build_skill_response(skill, raw_skill_name, tool_input)

        if catalog is not None:
            # Only an unresolved gobby-namespaced name reaches here with a catalog.
            context = f"Unknown Gobby skill {skill_name!r}.\n\n"
            context += capability_menu(catalog, "/gobby")
            context += "\n\nInstalled standalone skills:\n" + standalone_menu(
                self._router_skills(session_id, project_id), catalog, "/gobby"
            )
            context += "\nDiscover installed skills with list_skills on gobby-skills."
            return HookResponse(decision="block", reason="Unknown Gobby skill", context=context)

        return None

    def _build_skill_response(
        self,
        skill: ParsedSkill,
        raw_skill_name: str,
        tool_input: dict[str, Any],
    ) -> HookResponse:
        """Build a blocking response with a visible, level-aware skill load."""
        args = str(tool_input.get("args", "") or "")
        route = route_gobby_request(f"skill {skill.name} {args}", resolve_skill=lambda _: skill)
        context = route.context
        if args:
            context += f"\n\nUser arguments: {args}"
        self.logger.info(
            "Resolved gobby skill '%s' locally (requested: '%s')", skill.name, raw_skill_name
        )
        return HookResponse(
            decision="block",
            reason=f"Gobby skill '{skill.name}' resolved locally — fetch it with gobby-skills",
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
            self.logger.debug("AFTER_TOOL [%s]: %s, session %s", status, tool_name, session_id)
            self._resume_turn_lifecycle(event)
            if not is_wrapper_echo_event(event):
                try:
                    db = getattr(self._session_manager, "db", None)
                    if db is not None:
                        track_tool_outcome(
                            SessionVariableManager(db),
                            session_id,
                            event,
                        )
                except Exception as exc:
                    self.logger.debug(
                        "Failed to track native tool outcome for %s: %s",
                        tool_name,
                        exc,
                        exc_info=True,
                    )
            self._record_autonomous_tool_progress(event, session_id, tool_name)

            if self._session_manager is not None and self._agent_run_manager is not None:
                try:
                    schedule_terminal_handoff_delivery(
                        event,
                        session_manager=cast("SessionManager", self._session_manager),
                        agent_run_manager=self._agent_run_manager,
                        event_loop=self._event_loop,
                        terminal_manager=getattr(self, "terminal_manager", None),
                        terminal_runtime_registry=self._terminal_runtime_registry,
                    )
                except Exception:
                    self.logger.warning(
                        "Failed scheduling post-result terminal handoff delivery",
                        exc_info=True,
                    )

            # Structured edit tools and normalized shell writes both count as edits.
            is_canonical_edit = (
                input_data.get("canonical_tool_kind") == "write"
                and input_data.get("canonical_repo_mutation") is True
            )
            is_edit = str(tool_name).lower() in EDIT_TOOLS or is_canonical_edit

            if not is_failure and is_edit and self._session_manager:
                try:
                    self._record_successful_file_mutation(
                        event,
                        session_id,
                        is_canonical_edit=is_canonical_edit,
                    )
                except Exception as e:
                    # Don't fail the event if tracking fails
                    self.logger.warning("Failed to process file edit: %s", e, exc_info=True)

        else:
            self.logger.debug("AFTER_TOOL [%s]: %s", status, tool_name)

        return HookResponse(decision="allow")

    def _record_successful_file_mutation(
        self,
        event: HookEvent,
        session_id: str,
        *,
        is_canonical_edit: bool,
    ) -> None:
        """Persist one successful mutation observation and all committable paths."""
        input_data = event.data
        tool_input = input_data.get("tool_input")
        legacy_file_path = None
        if isinstance(tool_input, dict):
            legacy_file_path = (
                tool_input.get("file_path")
                or tool_input.get("target_file")
                or tool_input.get("path")
            )

        canonical_paths = input_data.get("canonical_file_paths")
        if is_canonical_edit and isinstance(canonical_paths, list):
            file_paths = list(
                dict.fromkeys(path for path in canonical_paths if isinstance(path, str) and path)
            )
        else:
            file_paths = [legacy_file_path] if isinstance(legacy_file_path, str) else []

        committable_paths: list[str] = []
        paths_by_checkout: dict[str, list[str]] = {}
        for file_path in file_paths:
            repo_edit = self._resolve_repo_edit_paths(
                file_path,
                event.cwd,
                project_id=event.project_id,
            )
            if repo_edit is None:
                continue
            repo_root, repo_relative_path = repo_edit

            if repo_relative_path not in committable_paths:
                committable_paths.append(repo_relative_path)
                # `.gobby` files participate in task ownership like every
                # other repository path, but are not source files for gcode.
                if Path(repo_relative_path).parts[:1] != (".gobby",):
                    self._notify_code_index(repo_root, repo_relative_path)
            checkout_paths = paths_by_checkout.setdefault(os.fspath(repo_root), [])
            if repo_relative_path not in checkout_paths:
                checkout_paths.append(repo_relative_path)

        structured_mutation = input_data.get("canonical_structured_mutation") is True
        if structured_mutation and not file_paths:
            self.logger.warning(
                "Successful structured mutation had no attributable file paths",
                extra={
                    "event": "file_mutation_attribution_unavailable",
                    "session_id": session_id,
                    "tool_name": input_data.get("tool_name"),
                    "mcp_server": input_data.get("mcp_server"),
                    "mcp_tool": input_data.get("mcp_tool"),
                },
            )
        if not committable_paths:
            return

        db = getattr(self._session_manager, "db", None)
        if db is not None:
            variable_manager = SessionVariableManager(db)
            edited_at = event.timestamp.timestamp()
            variables = variable_manager.get_variables(session_id)
            task_id = active_task_id_for_edit(variables)
            for checkout_root, paths in paths_by_checkout.items():
                if task_id is not None:
                    landed = self._paths_landed_before_edit(
                        variables, task_id, checkout_root, paths, edited_at
                    )
                    if landed:
                        self.logger.info(
                            "Skipping edit attribution for paths committed after the edit "
                            "event (replayed envelope)",
                            extra={
                                "event": "file_mutation_attribution_stale",
                                "session_id": session_id,
                                "task_id": task_id,
                                "checkout_root": checkout_root,
                                "paths": sorted(landed),
                            },
                        )
                        paths = [path for path in paths if path not in landed]
                if paths:
                    variable_manager.record_edited_files(
                        session_id,
                        paths,
                        checkout_root=checkout_root,
                        edited_at=edited_at,
                    )

        self._mark_session_had_edits_if_claimed(session_id)

    def _paths_landed_before_edit(
        self,
        variables: dict[str, Any],
        task_id: str,
        checkout_root: str,
        paths: list[str],
        edited_at: float,
    ) -> set[str]:
        """Return unattributed paths whose last commit postdates the edit event.

        An edit hook observed after a later commit touching the path is a replayed
        outage-queued envelope: its content is already landed, so attributing it
        would re-open the task ledger for committed work. Paths the task already
        holds are left to ``record_edited_files``, which keeps the newest stamp.

        This runs on a hook worker thread, so the supervised Git query is driven
        through the daemon loop. A bridge failure yields ``None``; keeping the
        attribution is the safe direction, the same as a Git timeout.
        """
        attributed = task_edited_file_set_for_checkout(variables, task_id, checkout_root)
        candidates = {path for path in paths if path not in attributed}
        if not candidates:
            return set()
        landed = run_coro_blocking(
            paths_committed_after_async(candidates, checkout_root, edited_at),
            self._event_loop,
            self.logger,
            label="paths_committed_after",
        )
        return landed if isinstance(landed, set) else set()

    def _notify_code_index(self, repo_root: Path, repo_relative_path: str) -> None:
        if self._code_index_trigger is None:
            return
        try:
            root_path = os.fspath(repo_root)
            project_id = self._resolve_project_id(None, root_path)
            if project_id:
                self._code_index_trigger.notify_file_changed(
                    file_path=repo_relative_path,
                    project_id=project_id,
                    root_path=root_path,
                    code_overlay_project_id=overlay_project_id_for_root(repo_root),
                )
        except Exception as exc:
            self.logger.debug("Failed to trigger code index update: %s", exc)

    def _mark_session_had_edits_if_claimed(self, session_id: str) -> None:
        session_manager = self._session_manager
        if self._task_manager is None or session_manager is None:
            return
        try:
            claimed_tasks = self._task_manager.list_tasks(claimed_by_session_id=session_id)
        except Exception as exc:
            self.logger.debug(
                "Failed to check claimed tasks for session %s: %s",
                session_id,
                exc,
            )
            return
        if claimed_tasks:
            session_manager.mark_had_edits(session_id)

    def _record_autonomous_tool_progress(
        self, event: HookEvent, session_id: str, tool_name: str
    ) -> None:
        """Feed live tool traffic to autonomous progress detection."""
        if self._progress_tracker is None:
            return
        try:
            self._progress_tracker.record_tool_call(
                session_id=session_id,
                tool_name=tool_name,
                tool_args=event.data.get("tool_input", {}),
                tool_result=(
                    event.data.get("tool_output")
                    or event.data.get("tool_response")
                    or event.data.get("tool_result")
                ),
            )
        except Exception as e:
            self.logger.warning("Failed to record autonomous tool progress: %s", e)

    def _resolve_repo_edit_paths(
        self,
        file_path: str,
        cwd: str | None,
        *,
        project_id: str | None = None,
    ) -> tuple[Path, str] | None:
        """Return ``(repo_root, repo_relative_path)`` for an edited file."""
        target_path = Path(os.path.expanduser(file_path))
        cwd_path = Path(cwd).resolve(strict=False) if cwd else None
        if target_path.is_absolute():
            resolved_target = target_path.resolve(strict=False)
            search_start = cwd_path or resolved_target.parent
        elif cwd_path is not None:
            resolved_target = (cwd_path / target_path).resolve(strict=False)
            search_start = cwd_path
        else:
            return None

        from gobby.utils.project_context import find_project_root, get_project_context

        # Without a project root or cwd there is no repo to attribute the edit
        # to — fabricating one from the file's parent would attribute scratchpad
        # and other out-of-repo writes as repo edits.
        repo_root = find_project_root(search_start) or cwd_path
        if repo_root is None:
            return None
        repo_root = repo_root.resolve(strict=False)
        if target_path.is_absolute() and not resolved_target.is_relative_to(repo_root):
            target_root = find_project_root(resolved_target.parent)
            target_context = get_project_context(target_root) if target_root is not None else None
            if (
                project_id is None
                or target_root is None
                or target_context is None
                or target_context.get("id") != project_id
            ):
                return None
            repo_root = target_root.resolve(strict=False)
        if not resolved_target.is_relative_to(repo_root):
            return None

        rel_path = os.path.normpath(os.fspath(resolved_target.relative_to(repo_root)))
        return repo_root, rel_path

    def handle_before_tool_selection(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_TOOL_SELECTION events."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug("BEFORE_TOOL_SELECTION: session %s", session_id)
        else:
            self.logger.debug("BEFORE_TOOL_SELECTION")

        return HookResponse(decision="allow")
