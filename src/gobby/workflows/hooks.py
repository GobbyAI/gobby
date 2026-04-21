import asyncio
import concurrent.futures
import json
import logging
import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.session_tasks import SessionTaskManager

    from .rule_engine import RuleEngine

logger = logging.getLogger(__name__)


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value == HookEventType.BEFORE_AGENT.value


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value in {HookEventType.AFTER_AGENT.value, HookEventType.STOP.value}


class WorkflowHookHandler:
    """Integrates RuleEngine into the HookManager.

    Runs built-in observer functions (task claim tracking, MCP call tracking,
    plan mode detection) BEFORE rule evaluation so that rule conditions like
    ``mcp_called()``, ``task_claimed``, and ``mode_level`` work correctly.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout: float = 30.0,
        enabled: bool = True,
        rule_engine: "RuleEngine | None" = None,
        task_manager: "LocalTaskManager | None" = None,
        session_task_manager: "SessionTaskManager | None" = None,
    ):
        self.rule_engine = rule_engine
        self._task_manager = task_manager
        self._session_task_manager = session_task_manager
        self._loop = loop
        self.timeout = timeout if timeout > 0 else None
        self._enabled = enabled

        # Session variable manager for persisting rule set_variable effects
        self._session_var_manager = None
        if rule_engine:
            from gobby.workflows.state_manager import SessionVariableManager

            self._session_var_manager = SessionVariableManager(rule_engine.db)

        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        # Codex can omit tool_input on AFTER_TOOL. Track the prior BEFORE_TOOL
        # context so progressive-discovery and task observers still see parity.
        self._codex_tool_context_lock = threading.Lock()
        self._codex_tool_contexts: dict[str, list[dict[str, Any]]] = {}
        self._codex_tool_context_by_id: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _codex_tool_context_ids(data: dict[str, Any]) -> list[str]:
        """Extract stable per-tool identifiers from Codex hook payloads."""
        identifiers: list[str] = []
        for key in (
            "tool_use_id",
            "toolUseId",
            "tool_call_id",
            "toolCallId",
            "call_id",
            "callId",
            "item_id",
            "itemId",
            "id",
        ):
            value = data.get(key)
            if isinstance(value, str) and value and value not in identifiers:
                identifiers.append(value)
        return identifiers

    @staticmethod
    def _needs_codex_tool_rehydration(data: dict[str, Any]) -> bool:
        """Return True when a Codex AFTER_TOOL event lacks usable tool context."""
        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return True

        if data.get("tool_input") in (None, "", {}):
            return True

        if tool_name.startswith("mcp__") and (
            not data.get("mcp_server") or not data.get("mcp_tool")
        ):
            return True

        return False

    def _snapshot_codex_tool_context(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Capture the Codex BEFORE_TOOL fields needed later on AFTER_TOOL."""
        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        snapshot: dict[str, Any] = {"tool_name": tool_name}
        for key in (
            "tool_input",
            "mcp_server",
            "mcp_tool",
            "item_id",
            "tool_use_id",
            "tool_call_id",
            "call_id",
        ):
            value = data.get(key)
            if value not in (None, ""):
                snapshot[key] = deepcopy(value)

        identifiers = self._codex_tool_context_ids(data)
        if identifiers:
            snapshot["_ids"] = identifiers

        return snapshot

    def _remember_codex_tool_context(self, session_id: str, data: dict[str, Any]) -> None:
        """Store Codex BEFORE_TOOL context until the matching AFTER_TOOL arrives."""
        snapshot = self._snapshot_codex_tool_context(data)
        if snapshot is None:
            return

        with self._codex_tool_context_lock:
            self._codex_tool_contexts.setdefault(session_id, []).append(snapshot)
            for identifier in snapshot.get("_ids", []):
                self._codex_tool_context_by_id[(session_id, identifier)] = snapshot

    def _match_codex_tool_context(
        self,
        session_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the best stored Codex BEFORE_TOOL context for an AFTER_TOOL event."""
        with self._codex_tool_context_lock:
            for identifier in self._codex_tool_context_ids(data):
                snapshot = self._codex_tool_context_by_id.get((session_id, identifier))
                if snapshot is not None:
                    return snapshot

            pending = self._codex_tool_contexts.get(session_id, [])
            if not pending:
                return None

            tool_name = data.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                for snapshot in reversed(pending):
                    if snapshot.get("tool_name") == tool_name:
                        return snapshot

            return pending[-1]

    def _forget_codex_tool_context(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Remove stored Codex BEFORE_TOOL context after the tool completes."""
        with self._codex_tool_context_lock:
            pending = self._codex_tool_contexts.get(session_id, [])
            if snapshot in pending:
                pending.remove(snapshot)
                if not pending:
                    self._codex_tool_contexts.pop(session_id, None)

            for identifier in snapshot.get("_ids", []):
                self._codex_tool_context_by_id.pop((session_id, identifier), None)

    def _clear_codex_tool_context(self, session_id: str) -> None:
        """Drop any stored Codex tool context for a session."""
        with self._codex_tool_context_lock:
            snapshots = self._codex_tool_contexts.pop(session_id, [])
            for snapshot in snapshots:
                for identifier in snapshot.get("_ids", []):
                    self._codex_tool_context_by_id.pop((session_id, identifier), None)

    def _sync_codex_tool_context(self, event: HookEvent, session_id: str) -> None:
        """Maintain Codex BEFORE/AFTER tool parity for rule evaluation."""
        if (
            event.source != SessionSource.CODEX
            or not session_id
            or not isinstance(event.data, dict)
        ):
            return

        if event.event_type == HookEventType.SESSION_END:
            self._clear_codex_tool_context(session_id)
            return

        if event.event_type == HookEventType.BEFORE_TOOL:
            self._remember_codex_tool_context(session_id, event.data)
            return

        if event.event_type != HookEventType.AFTER_TOOL:
            return

        snapshot = self._match_codex_tool_context(session_id, event.data)
        if snapshot is None:
            return

        if self._needs_codex_tool_rehydration(event.data):
            for key, value in snapshot.items():
                if key.startswith("_"):
                    continue
                if event.data.get(key) in (None, "", {}):
                    event.data[key] = deepcopy(value)

            from gobby.hooks.normalization import normalize_tool_fields

            normalize_tool_fields(event.data)
            event.metadata["_codex_tool_context_rehydrated"] = True

        self._forget_codex_tool_context(session_id, snapshot)

    def _resolve_project_path(self, event: HookEvent) -> str | None:
        """Resolve the best available filesystem path for workflow git checks."""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata is not event.metadata:
            event.metadata = metadata

        project_path = event.cwd if event.cwd and event.cwd.strip() else None
        if project_path:
            return project_path

        metadata_path = metadata.get("project_path")
        if isinstance(metadata_path, str) and metadata_path.strip():
            return metadata_path

        if not event.project_id or self.rule_engine is None:
            return None

        try:
            from gobby.storage.projects import LocalProjectManager

            project = LocalProjectManager(self.rule_engine.db).get(event.project_id)
        except Exception as exc:
            logger.debug(
                "Failed to resolve project_path from project_id=%s: %s",
                event.project_id,
                exc,
            )
            return None

        repo_path = project.repo_path if project is not None else None
        if not isinstance(repo_path, str) or not repo_path.strip():
            return None

        metadata.setdefault("project_path", repo_path)
        return repo_path

    def _handle_cancelled(self, event: HookEvent) -> HookResponse:
        """Handle CancelledError by logging and returning appropriate response."""
        logger.warning(f"Workflow evaluation cancelled for {event.event_type}")
        if event.event_type == HookEventType.STOP:
            return HookResponse(
                decision="block",
                reason="Workflow evaluation was cancelled; blocking stop for safety.",
            )
        return HookResponse(decision="allow")

    def _run_observers(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> None:
        """Run built-in observer functions to populate tracking variables.

        Must run BEFORE rule evaluation so conditions have current data.
        """
        from .observers import (
            detect_bash_commit,
            detect_commit_link,
            detect_mcp_call,
            detect_plan_mode_from_context,
            detect_task_claim,
            reconcile_claimed_tasks,
        )

        # Reconcile stale claimed_tasks on semantic turn-end before rule evaluation
        if _is_turn_end_event(event.event_type):
            reconcile_claimed_tasks(
                variables,
                session_id,
                task_manager=self._task_manager,
            )

        # Task claim/release tracking (AFTER_TOOL for gobby-tasks calls)
        if event.event_type == HookEventType.AFTER_TOOL:
            detect_task_claim(
                event,
                variables,
                session_id,
                session_task_manager=self._session_task_manager,
                task_manager=self._task_manager,
                project_id=event.project_id,
            )
            detect_commit_link(event, variables, session_id)
            detect_bash_commit(event, variables, session_id)
            detect_mcp_call(event, variables, session_id)

        # Plan mode detection on the semantic start-of-turn boundary
        if _is_turn_start_event(event.event_type):
            prompt = (event.data or {}).get("prompt", "") or ""
            if prompt:
                detect_plan_mode_from_context(prompt, variables, session_id)

    async def _evaluate_rules(self, event: HookEvent) -> HookResponse:
        """Evaluate rules for a hook event using the RuleEngine.

        Loads variables, runs observers to populate tracking state,
        then evaluates rules. Persists any changed variables afterward.
        """
        if self.rule_engine is None:
            return HookResponse(decision="allow")

        try:
            session_id = event.metadata.get("_platform_session_id") or event.session_id or ""

            self._sync_codex_tool_context(event, session_id)

            # Load session-scoped variables (canonical store)
            variables: dict[str, Any] = {}
            if self._session_var_manager:
                try:
                    variables = dict(self._session_var_manager.get_variables(session_id))
                except Exception as e:
                    if event.event_type == HookEventType.STOP:
                        logger.warning(
                            f"Failed to load session variables on STOP - blocking for safety: {e}",
                        )
                        return HookResponse(
                            decision="block",
                            reason="Could not load session state. Try again.",
                        )
                    logger.debug(f"Could not load session variables for rules: {e}")

            # Inject current_step from active workflow instance so rule templates
            # can display it (e.g., require-step-completion block message).
            if variables.get("is_spawned_agent") and not variables.get("current_step"):
                try:
                    from gobby.workflows.state_manager import WorkflowInstanceManager

                    instances = WorkflowInstanceManager(self.rule_engine.db).get_active_instances(
                        session_id
                    )
                    for inst in instances:
                        if inst.current_step:
                            variables["current_step"] = inst.current_step
                            break
                except Exception as e:
                    logger.debug(f"Could not inject current_step from workflow instance: {e}")

            # Lazy-init variable presets for sessions that started before gobby init.
            # Mirrors the baseline_dirty_files pattern below — one-time DB hit per session.
            if "_variable_defaults_loaded" not in variables and event.project_id:
                try:
                    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

                    def_manager = LocalWorkflowDefinitionManager(self.rule_engine.db)
                    enabled_variables = [
                        v for v in def_manager.list_all(workflow_type="variable") if v.enabled
                    ]
                    defaults: dict[str, Any] = {}
                    for var_row in enabled_variables:
                        try:
                            var_body = json.loads(var_row.definition_json)
                            key = var_body.get("variable", var_row.name)
                            if key not in variables:
                                defaults[key] = var_body.get("value")
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    defaults["_variable_defaults_loaded"] = True
                    variables.update(defaults)
                    if self._session_var_manager and session_id:
                        self._session_var_manager.merge_variables(session_id, defaults)
                except Exception as e:
                    logger.debug(f"Could not lazy-load variable defaults: {e}")

            from gobby.workflows.git_utils import get_dirty_files_categorized
            from gobby.workflows.safe_evaluator import LazyBool

            project_path = self._resolve_project_path(event)
            if not project_path:
                logger.warning(
                    f"_evaluate_rules: no project_path resolved for session={session_id} "
                    f"event={event.event_type} source={event.source} "
                    f"cwd={event.cwd!r} project_id={event.project_id!r} "
                    f"metadata_path={event.metadata.get('project_path')!r}"
                )

            # Lazy-init baseline on first evaluation (rule template may not have fired)
            if "baseline_dirty_files" not in variables:
                initial_dirty = sorted(get_dirty_files_categorized(project_path).all)
                variables["baseline_dirty_files"] = initial_dirty
                variables.setdefault("session_edited_files", [])
                # Persist so future evaluations have it
                if self._session_var_manager and session_id:
                    self._session_var_manager.merge_variables(
                        session_id,
                        {"baseline_dirty_files": initial_dirty, "session_edited_files": []},
                    )

            session_edited = set(variables.get("session_edited_files", []))

            def _check_dirty(
                _edited: set[str] = session_edited,
                _path: str | None = project_path,
            ) -> bool:
                result = get_dirty_files_categorized(_path)
                # Only count files this session actually touched
                dirty_tracked = result.tracked
                dirty_untracked = result.untracked
                session_dirty_tracked = _edited & dirty_tracked
                session_dirty_untracked = _edited & dirty_untracked
                return bool(session_dirty_tracked or session_dirty_untracked)

            eval_context = {"has_dirty_files": LazyBool(_check_dirty)}

            # Snapshot BEFORE observers to capture both observer and rule changes in the diff
            pre_eval = deepcopy(variables)

            # Run built-in observers BEFORE rule evaluation
            self._run_observers(event, session_id, variables)

            response = await self.rule_engine.evaluate(
                event=event,
                session_id=session_id,
                variables=variables,
                eval_context=eval_context,
            )

            # Persist all variables changed by observers OR rule effects
            if self._session_var_manager:
                changed = {
                    k: v for k, v in variables.items() if k not in pre_eval or pre_eval[k] != v
                }
                if changed:
                    self._session_var_manager.merge_variables(session_id, changed)

            return response
        except Exception as e:
            logger.error(f"RuleEngine evaluation failed: {e}", exc_info=True)
            raise

    def evaluate(self, event: HookEvent) -> HookResponse:
        """Evaluate rules for a hook event.

        Primary entry point for workflow evaluation.
        """
        if not self._enabled:
            return HookResponse(decision="allow")

        try:
            if self._loop and self._loop.is_running():
                if threading.current_thread() is threading.main_thread():
                    return HookResponse(decision="allow")
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        self._evaluate_rules(event),
                        self._loop,
                    )
                    return future.result(timeout=self.timeout)

            try:
                asyncio.get_running_loop()
                logger.warning("Could not run workflow engine: Event loop is already running.")
                return HookResponse(decision="allow")
            except RuntimeError:
                return asyncio.run(self._evaluate_rules(event))

        except concurrent.futures.CancelledError:
            return self._handle_cancelled(event)
        except Exception as e:
            logger.error(f"Error evaluating rules: {e}", exc_info=True)
            raise

    def handle(self, event: HookEvent) -> HookResponse:
        """Handle a hook event by evaluating declarative rules."""
        return self.evaluate(event)
