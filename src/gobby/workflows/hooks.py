import asyncio
import concurrent.futures
import json
import logging
import threading
from _thread import LockType
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.projects import GLOBAL_PROJECT_ID, ORPHANED_PROJECT_ID, PERSONAL_PROJECT_ID
from gobby.workflows.step_context import get_active_step_workflow_context

if TYPE_CHECKING:
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

    from .engine import RuleEngine

logger = logging.getLogger(__name__)

_NO_REPO_PROJECT_CONSTANTS = frozenset(
    {
        PERSONAL_PROJECT_ID,
        GLOBAL_PROJECT_ID,
        ORPHANED_PROJECT_ID,
    }
)
_NO_REPO_LEGACY_PROJECT_IDS = frozenset({"_personal", "_global", "_orphaned"})
_NO_REPO_SYSTEM_PROJECTS = frozenset(
    {
        *_NO_REPO_PROJECT_CONSTANTS,
        *_NO_REPO_LEGACY_PROJECT_IDS,
        "_migrated",
    }
)

_TOOL_CONTEXT_REHYDRATION_SOURCES = frozenset(
    {
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.GEMINI,
        SessionSource.QWEN,
        SessionSource.DROID,
    }
)


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value == HookEventType.BEFORE_AGENT.value


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value in {HookEventType.AFTER_AGENT.value, HookEventType.STOP.value}


def _is_known_no_repo_project(project_id: str | None) -> bool:
    return isinstance(project_id, str) and project_id in _NO_REPO_SYSTEM_PROJECTS


class _EvalLockState:
    """Per-session evaluation lock plus registry bookkeeping."""

    lock: LockType
    references: int
    cleanup_pending: bool

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.references = 0
        self.cleanup_pending = False


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
        session_manager: "SessionManager | None" = None,
        session_task_manager: "SessionTaskManager | None" = None,
    ):
        self.rule_engine = rule_engine
        self._task_manager = task_manager
        self._session_manager = session_manager
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

        # Some CLIs omit tool_input on AFTER_TOOL. Track the prior BEFORE_TOOL
        # context so progressive-discovery and task observers still see parity.
        self._tool_context_lock = threading.Lock()
        self._tool_contexts: dict[str, list[dict[str, Any]]] = {}
        self._tool_context_by_id: dict[tuple[str, str], dict[str, Any]] = {}

        self._eval_locks_lock = threading.Lock()
        self._eval_locks: dict[str, _EvalLockState] = {}

    @staticmethod
    def _tool_context_ids(data: dict[str, Any]) -> list[str]:
        """Extract stable per-tool identifiers from hook payloads."""
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
    def _tool_context_fingerprint(data: dict[str, Any]) -> str | None:
        """Return a content fingerprint for matching direct MCP proxy re-entry."""
        tool_name = data.get("tool_name") or data.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        tool_input = data.get("tool_input") or data.get("toolInput") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_input = deepcopy(tool_input)
        for arg_key in ("arguments", "args"):
            raw_args = tool_input.get(arg_key)
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(parsed_args, dict):
                    tool_input[arg_key] = parsed_args

        try:
            input_json = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
        except TypeError:
            input_json = repr(tool_input)
        return f"{tool_name}:{input_json}"

    @staticmethod
    def _needs_tool_rehydration(data: dict[str, Any]) -> bool:
        """Return True when an AFTER_TOOL event lacks usable tool context."""
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

    def _snapshot_tool_context(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Capture BEFORE_TOOL fields needed later on AFTER_TOOL."""
        tool_name = data.get("tool_name") or data.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        snapshot: dict[str, Any] = {"tool_name": tool_name}
        for key in (
            "tool_input",
            "mcp_server",
            "mcp_tool",
            "item_id",
            "itemId",
            "tool_use_id",
            "toolUseId",
            "tool_call_id",
            "toolCallId",
            "call_id",
            "callId",
            "id",
        ):
            value = data.get(key)
            if value not in (None, ""):
                snapshot[key] = deepcopy(value)

        identifiers = self._tool_context_ids(data)
        if identifiers:
            snapshot["_ids"] = identifiers

        fingerprint = self._tool_context_fingerprint(data)
        if fingerprint:
            snapshot["_fingerprint"] = fingerprint

        return snapshot

    @staticmethod
    def _tool_context_session_key(source: SessionSource, session_id: str) -> str:
        """Build a cache key that keeps CLI sources isolated."""
        return f"{source.value}:{session_id}"

    def _remember_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> None:
        """Store BEFORE_TOOL context until the matching AFTER_TOOL arrives."""
        snapshot = self._snapshot_tool_context(data)
        if snapshot is None:
            return

        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            self._tool_contexts.setdefault(cache_key, []).append(snapshot)
            for identifier in snapshot.get("_ids", []):
                self._tool_context_by_id[(cache_key, identifier)] = snapshot

    def _match_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the best stored BEFORE_TOOL context for an AFTER_TOOL event."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            for identifier in self._tool_context_ids(data):
                snapshot = self._tool_context_by_id.get((cache_key, identifier))
                if snapshot is not None:
                    return snapshot

            pending = self._tool_contexts.get(cache_key, [])
            if not pending:
                return None

            tool_name = data.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                for snapshot in reversed(pending):
                    if snapshot.get("tool_name") == tool_name:
                        return snapshot

            return pending[-1]

    def _forget_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Remove stored BEFORE_TOOL context after the tool completes."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            pending = self._tool_contexts.get(cache_key, [])
            if snapshot in pending:
                pending.remove(snapshot)
                if not pending:
                    self._tool_contexts.pop(cache_key, None)

            for identifier in snapshot.get("_ids", []):
                self._tool_context_by_id.pop((cache_key, identifier), None)

    def _clear_tool_context(self, source: SessionSource, session_id: str) -> None:
        """Drop any stored tool context for a session."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            snapshots = self._tool_contexts.pop(cache_key, [])
            for snapshot in snapshots:
                for identifier in snapshot.get("_ids", []):
                    self._tool_context_by_id.pop((cache_key, identifier), None)

    def has_pending_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Return whether a matching CLI BEFORE_TOOL context is still pending."""
        fingerprint = self._tool_context_fingerprint(data)
        if not fingerprint:
            return False

        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            return any(
                snapshot.get("_fingerprint") == fingerprint
                for snapshot in self._tool_contexts.get(cache_key, [])
            )

    def _sync_tool_context(self, event: HookEvent, session_id: str) -> None:
        """Maintain BEFORE/AFTER tool parity for rule evaluation."""
        if (
            event.source not in _TOOL_CONTEXT_REHYDRATION_SOURCES
            or not session_id
            or not isinstance(event.data, dict)
        ):
            return

        if event.event_type == HookEventType.SESSION_END:
            self._clear_tool_context(event.source, session_id)
            return

        if event.event_type == HookEventType.BEFORE_TOOL:
            self._remember_tool_context(event.source, session_id, event.data)
            return

        if event.event_type != HookEventType.AFTER_TOOL:
            return

        snapshot = self._match_tool_context(event.source, session_id, event.data)
        if snapshot is None:
            return

        if self._needs_tool_rehydration(event.data):
            for key, value in snapshot.items():
                if key.startswith("_"):
                    continue
                if event.data.get(key) in (None, "", {}):
                    event.data[key] = deepcopy(value)

            from gobby.hooks.normalization import normalize_tool_fields

            normalize_tool_fields(event.data)
            event.metadata["_tool_context_rehydrated"] = True
            event.metadata["_tool_context_rehydrated_source"] = event.source.value
            if event.source == SessionSource.CODEX:
                event.metadata["_codex_tool_context_rehydrated"] = True

        self._forget_tool_context(event.source, session_id, snapshot)

    def _reserve_eval_lock(self, session_id: str) -> _EvalLockState:
        """Reserve the per-session evaluation lock for one active or waiting event."""
        with self._eval_locks_lock:
            state = self._eval_locks.get(session_id)
            if state is None:
                state = _EvalLockState()
                self._eval_locks[session_id] = state
            state.references += 1
            return state

    def _release_eval_lock(
        self,
        session_id: str,
        state: _EvalLockState,
        *,
        cleanup: bool = False,
    ) -> None:
        """Release one registry reference and prune ended sessions after waiters drain."""
        with self._eval_locks_lock:
            current = self._eval_locks.get(session_id)
            if current is not state:
                return
            if cleanup:
                state.cleanup_pending = True
            state.references -= 1
            if state.references <= 0 and state.cleanup_pending:
                self._eval_locks.pop(session_id, None)

    async def _acquire_eval_lock(self, lock: LockType) -> None:
        """Acquire a thread lock without blocking the event loop."""
        if lock.acquire(blocking=False):
            return

        acquire_task = asyncio.create_task(asyncio.to_thread(lock.acquire))
        try:
            await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            await acquire_task
            lock.release()
            raise

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
            detect_verification_evidence,
            reconcile_claimed_tasks,
        )

        # Reconcile stale claimed_tasks on semantic turn-end before rule evaluation
        if _is_turn_end_event(event.event_type):
            reconcile_claimed_tasks(
                variables,
                session_id,
                task_manager=self._task_manager,
                session_manager=self._session_manager,
                session_task_manager=self._session_task_manager,
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
            detect_verification_evidence(event, variables, session_id)
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
            eval_lock_state = self._reserve_eval_lock(session_id) if session_id else None
            eval_lock_acquired = False

            try:
                if eval_lock_state:
                    await self._acquire_eval_lock(eval_lock_state.lock)
                    eval_lock_acquired = True

                self._sync_tool_context(event, session_id)
                if isinstance(event.data, dict) and not event.metadata.get(
                    "_tool_context_rehydrated"
                ):
                    from gobby.hooks.normalization import normalize_tool_fields

                    normalize_tool_fields(event.data)

                # Load session-scoped variables (canonical store)
                variables: dict[str, Any] = {}
                if self._session_var_manager:
                    try:
                        variables = dict(self._session_var_manager.get_variables(session_id))
                    except Exception as e:
                        if event.event_type == HookEventType.STOP:
                            logger.warning(
                                "Failed to load session variables on STOP - "
                                f"blocking for safety: {e}",
                            )
                            return HookResponse(
                                decision="block",
                                reason="Could not load session state. Try again.",
                            )
                        logger.debug(f"Could not load session variables for rules: {e}")

                # Inject active step details so stop gates can give actionable
                # lifecycle instructions.
                if variables.get("is_spawned_agent"):
                    try:
                        step_context = await asyncio.to_thread(
                            get_active_step_workflow_context,
                            self.rule_engine.db,
                            session_id,
                        )
                        if step_context is not None:
                            variables["current_step"] = step_context.current_step
                            variables.setdefault("_step_workflow_name", step_context.workflow_name)
                            if step_context.status_message:
                                variables["current_step_status_message"] = (
                                    step_context.status_message
                                )
                            if step_context.description:
                                variables["current_step_description"] = step_context.description
                    except Exception as e:
                        logger.debug(f"Could not inject current_step from workflow instance: {e}")

                # Lazy-init variable presets for sessions that started before gobby init.
                # Mirrors the baseline_dirty_files pattern below — one-time DB hit per session.
                if "_variable_defaults_loaded" not in variables and event.project_id:
                    try:
                        from gobby.storage.workflow_definitions import (
                            LocalWorkflowDefinitionManager,
                        )

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
                    message = (
                        f"_evaluate_rules: no project_path resolved for session={session_id} "
                        f"event={event.event_type} source={event.source} "
                        f"cwd={event.cwd!r} project_id={event.project_id!r} "
                        f"metadata_path={event.metadata.get('project_path')!r}"
                    )
                    if _is_known_no_repo_project(event.project_id):
                        logger.debug(message)
                    else:
                        logger.warning(message)

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
            finally:
                if eval_lock_state:
                    if eval_lock_acquired:
                        eval_lock_state.lock.release()
                    self._release_eval_lock(
                        session_id,
                        eval_lock_state,
                        cleanup=event.event_type == HookEventType.SESSION_END,
                    )
        except Exception as e:
            logger.error(f"RuleEngine evaluation failed: {e}", exc_info=True)
            raise

    async def evaluate_async(self, event: HookEvent) -> HookResponse:
        """Evaluate rules asynchronously for callers that already own the loop."""
        if not self._enabled:
            return HookResponse(decision="allow")
        return await self._evaluate_rules(event)

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
                coroutine = self.evaluate_async(event)
                return asyncio.run(coroutine)

        except concurrent.futures.CancelledError:
            return self._handle_cancelled(event)
        except Exception as e:
            logger.error(f"Error evaluating rules: {e}", exc_info=True)
            raise

    def handle(self, event: HookEvent) -> HookResponse:
        """Handle a hook event by evaluating declarative rules."""
        return self.evaluate(event)
