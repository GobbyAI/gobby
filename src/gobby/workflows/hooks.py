import asyncio
import concurrent.futures
import json
import logging
import threading
from _thread import LockType
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from psycopg.errors import QueryCanceled

from gobby.hooks.effect_deadline import (
    BlockingEffectDeadline,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD, record_worker_staging
from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadlineExceeded,
    database_operation_deadline,
)
from gobby.storage.projects import GLOBAL_PROJECT_ID, ORPHANED_PROJECT_ID, PERSONAL_PROJECT_ID
from gobby.workflows.block_audit import audit_source_block, audit_source_block_sync
from gobby.workflows.enforcement.blocking import is_gobby_call_tool
from gobby.workflows.engine.event_utils import _get_tool_identity
from gobby.workflows.evaluation_runtime import WorkflowEvaluationTimeout
from gobby.workflows.found_work_gate import (
    FOUND_WORK_GATE_ARMED_AT_VARIABLE,
    FoundWorkStopAnalyzer,
    capture_found_work_handoff,
    capture_turn_prompt,
    is_found_work_deferral,
)
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.tool_context import WorkflowToolContextMixin

if TYPE_CHECKING:
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime

    from .engine import RuleEngine

logger = logging.getLogger(__name__)

DEFAULT_EVALUATION_TIMEOUT_SECONDS = 15.0
_DATABASE_TIMEOUTS = (DatabaseOperationDeadlineExceeded, QueryCanceled)


# Keeps the runtime wait ordered between the evaluation deadline it backstops
# and the adapter timeout enclosing it.
_RUNTIME_WAIT_MARGIN_SECONDS = 1.0

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


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value == HookEventType.BEFORE_AGENT.value


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value in {HookEventType.AFTER_AGENT.value, HookEventType.STOP.value}


def _is_known_no_repo_project(project_id: str | None) -> bool:
    return isinstance(project_id, str) and project_id in _NO_REPO_SYSTEM_PROJECTS


def _target_task_tool_input(data: dict[str, Any]) -> dict[str, Any]:
    raw_tool_input = data.get("tool_input") or data.get("arguments") or {}
    if not isinstance(raw_tool_input, dict):
        return {}

    tool_name = data.get("tool_name", "")
    if is_gobby_call_tool(tool_name):
        inner_args = raw_tool_input.get("arguments")
        if isinstance(inner_args, dict):
            return inner_args
        if isinstance(inner_args, str):
            try:
                parsed = json.loads(inner_args)
            except (json.JSONDecodeError, TypeError):
                return {}
            if isinstance(parsed, dict):
                return parsed
    return raw_tool_input


def _target_task_id_for_event(event: HookEvent, variables: dict[str, Any]) -> str | None:
    if not isinstance(event.data, dict):
        return None

    from gobby.workflows.task_claim_state import resolve_target_task_id

    return resolve_target_task_id(variables, _target_task_tool_input(event.data).get("task_id"))


class _EvalLockState:
    """Per-session evaluation lock plus registry bookkeeping."""

    lock: LockType
    references: int
    cleanup_pending: bool

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.references = 0
        self.cleanup_pending = False


class WorkflowHookHandler(WorkflowToolContextMixin):
    """Integrates RuleEngine into the HookManager.

    Runs built-in observer functions (task claim tracking, MCP call tracking,
    plan mode detection) BEFORE rule evaluation so that rule conditions like
    ``mcp_called()``, ``task_claimed``, and ``mode_level`` work correctly.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_EVALUATION_TIMEOUT_SECONDS,
        enabled: bool = True,
        rule_engine: "RuleEngine | None" = None,
        task_manager: "LocalTaskManager | None" = None,
        session_manager: "SessionManager | None" = None,
        session_task_manager: "SessionTaskManager | None" = None,
        config: Any | None = None,
        config_resolver: Callable[[], Any | None] | None = None,
        llm_service_resolver: Callable[[], Any | None] | None = None,
        evaluation_runtime: "WorkflowEvaluationRuntime | None" = None,
    ):
        self.rule_engine = rule_engine
        self._task_manager = task_manager
        self._session_manager = session_manager
        self._session_task_manager = session_task_manager
        self._config = config
        self._config_resolver = config_resolver or (lambda: self._config)
        self._found_work_analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=llm_service_resolver or (lambda: None),
            config_resolver=self._config_resolver,
            session_manager=session_manager,
            session_task_manager=session_task_manager,
            db=rule_engine.db if rule_engine is not None else None,
        )
        self._evaluation_runtime = evaluation_runtime
        self.timeout = timeout if timeout > 0 else None
        self._enabled = enabled

        # Session variable manager for persisting rule set_variable effects
        self._session_var_manager = None
        if rule_engine:
            from gobby.workflows.state_manager import SessionVariableManager

            self._session_var_manager = SessionVariableManager(rule_engine.db)

        # Some CLIs omit tool_input on AFTER_TOOL. Track the prior BEFORE_TOOL
        # context so progressive-discovery and task observers still see parity.
        self._initialize_tool_context_tracking()

        self._eval_locks_lock = threading.Lock()
        self._eval_locks: dict[str, _EvalLockState] = {}

    def _resolve_policy(self) -> tuple[bool, float | None]:
        config = self._config_resolver()
        if config is None:
            return self._enabled, self.timeout
        timeout = config.workflow.timeout
        return config.workflow.enabled, timeout if timeout > 0 else None

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
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)

    def _resolve_project_path(self, event: HookEvent, worktree_root: str | None) -> str | None:
        """Resolve the best available filesystem path for workflow git checks."""
        from gobby.storage.project_checkouts import (
            CheckoutNotFoundError,
            CheckoutSentinelRejectedError,
            MissingMachineContextError,
            OverlayRegistrationRejectedError,
            require_root,
            resolve_operation_root,
        )
        from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
        from gobby.storage.workspace_machine_scope import (
            MachineOwnershipMismatchError,
            require_local_machine_id,
        )

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata is not event.metadata:
            event.metadata = metadata
        if worktree_root:
            metadata["project_path"] = worktree_root
        project_id = event.project_id
        if (
            not project_id
            or self.rule_engine is None
            or _is_known_no_repo_project(project_id)
            or project_id in CHECKOUT_FREE_PROJECT_IDS
        ):
            return worktree_root

        # Checkout lookups fail soft on the hook hot path: a missing row, an
        # unregistered root, or a machine-context gap degrades to the git
        # worktree root (or None) so rule evaluation still runs.
        soft_errors = (
            CheckoutNotFoundError,
            CheckoutSentinelRejectedError,
            MissingMachineContextError,
            OverlayRegistrationRejectedError,
            MachineOwnershipMismatchError,
        )
        db = self.rule_engine.db
        try:
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=project_id
            )
            primary = require_root(db, project_id, machine_id)
        except soft_errors as exc:
            logger.debug("No checkout for project %s; using %r: %s", project_id, worktree_root, exc)
            return worktree_root
        if not worktree_root or Path(worktree_root).resolve() == Path(primary).resolve():
            metadata["project_path"] = primary
            return primary
        try:
            resolved = resolve_operation_root(
                db, project_id, machine_id, overlay_path=worktree_root
            )
        except soft_errors as exc:
            logger.debug("Unregistered root %s for project %s: %s", worktree_root, project_id, exc)
            return worktree_root
        metadata["project_path"] = resolved
        return resolved

    def _handle_cancelled(self, event: HookEvent) -> HookResponse:
        """Handle CancelledError by logging and returning appropriate response."""
        controlled_shutdown = (
            self._evaluation_runtime is not None and self._evaluation_runtime.is_closing is True
        )
        log_cancelled = (
            logger.debug
            if controlled_shutdown and event.event_type != HookEventType.STOP
            else logger.warning
        )
        log_cancelled("Workflow evaluation cancelled for %s", event.event_type)
        if event.event_type == HookEventType.STOP:
            response = HookResponse(
                decision="block",
                reason="Workflow evaluation was cancelled; blocking stop for safety.",
            )
            audit_source_block_sync(
                self,
                event,
                rule_id="workflow-evaluation-cancelled",
                reason=response.reason or "",
            )
            return response
        return HookResponse(decision="allow")

    def _run_observers(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> set[str]:
        """Run built-in observer functions to populate tracking variables.

        Must run BEFORE rule evaluation so conditions have current data.
        """
        from .observer_context_usage import (
            detect_context_compact_guidance,
            detect_mid_turn_context_compact_guidance,
        )
        from .observer_plan_mode import reconcile_native_mode, resolve_plan_mode
        from .observers import (
            detect_bash_commit,
            detect_commit_link,
            detect_mcp_call,
            detect_task_claim,
            detect_turn_interrupt,
            reconcile_claimed_tasks,
        )

        failures: set[str] = set()

        def run_observer(
            name: str,
            observer: Callable[..., object],
            *args: Any,
            **kwargs: Any,
        ) -> None:
            try:
                observer(*args, **kwargs)
            except _DATABASE_TIMEOUTS:
                raise
            except Exception:
                failures.add(name)
                logger.warning(
                    "Observer %s failed for session=%s event=%s",
                    name,
                    session_id,
                    event.event_type,
                    exc_info=True,
                )

        run_observer("detect_turn_interrupt", detect_turn_interrupt, event, variables)

        # Tool and stop payloads carry the provider's live permission mode;
        # turn-start events (e.g. Claude UserPromptSubmit) omit it and manual
        # plan-mode toggles fire no hook, so these events are the only
        # authoritative correction point for a stale plan_mode.
        if event.event_type in (
            HookEventType.BEFORE_TOOL,
            HookEventType.AFTER_TOOL,
        ) or _is_turn_end_event(event.event_type):
            run_observer(
                "reconcile_native_mode",
                reconcile_native_mode,
                event,
                variables,
                session_id,
            )

        # SessionStart is the hydration boundary after resume/compaction. Reconcile
        # there so the first tool gate sees authoritative DB claims.
        if event.event_type == HookEventType.SESSION_START or _is_turn_end_event(event.event_type):
            run_observer(
                "reconcile_claimed_tasks",
                reconcile_claimed_tasks,
                variables,
                session_id,
                task_manager=self._task_manager,
                session_manager=self._session_manager,
                session_task_manager=self._session_task_manager,
            )

        # Task claim/release tracking (AFTER_TOOL for gobby-tasks calls)
        if event.event_type == HookEventType.AFTER_TOOL:
            run_observer(
                "detect_task_claim",
                detect_task_claim,
                event,
                variables,
                session_id,
                session_task_manager=self._session_task_manager,
                task_manager=self._task_manager,
                project_id=event.project_id,
            )
            run_observer("detect_commit_link", detect_commit_link, event, variables, session_id)
            run_observer("detect_bash_commit", detect_bash_commit, event, variables, session_id)
            run_observer("detect_mcp_call", detect_mcp_call, event, variables, session_id)
            run_observer("capture_found_work_handoff", capture_found_work_handoff, event, variables)
            run_observer(
                "detect_mid_turn_context_compact_guidance",
                detect_mid_turn_context_compact_guidance,
                event,
                variables,
                session_id,
                self._session_manager,
                config=getattr(self._config_resolver(), "context_handoff", None),
            )

        # Plan mode detection on the semantic start-of-turn boundary
        if _is_turn_start_event(event.event_type):
            run_observer("capture_turn_prompt", capture_turn_prompt, event, variables)
            run_observer(
                "resolve_plan_mode",
                resolve_plan_mode,
                event,
                variables,
                session_id,
                self._session_manager,
            )
            run_observer(
                "detect_context_compact_guidance",
                detect_context_compact_guidance,
                variables,
                session_id,
                self._session_manager,
                config=getattr(self._config_resolver(), "context_handoff", None),
            )

        return failures

    async def _evaluate_rules(
        self,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        """Evaluate rules for a hook event using the RuleEngine.

        Loads variables, runs observers to populate tracking state,
        then evaluates rules. Persists any changed variables afterward.
        """
        if self.rule_engine is None:
            return HookResponse(decision="allow")

        try:
            # Canonical internal session id only. event.session_id is the
            # provider external id and must never key session-scoped
            # persistence; "" disables every session-scoped write below.
            session_id = event.metadata.get("_platform_session_id") or ""
            eval_lock_state = self._reserve_eval_lock(session_id) if session_id else None
            eval_lock_acquired = False

            try:
                if eval_lock_state:
                    if blocking_deadline is not None:
                        lock_wait_started = monotonic()
                        await self._acquire_eval_lock(eval_lock_state.lock)
                        blocking_deadline.extend(monotonic() - lock_wait_started)
                    else:
                        await self._acquire_eval_lock(eval_lock_state.lock)
                    eval_lock_acquired = True

                self._sync_tool_context(event, session_id)
                if isinstance(event.data, dict) and not event.metadata.get(
                    "_tool_context_rehydrated"
                ):
                    from gobby.hooks.normalization import normalize_tool_fields

                    normalize_tool_fields(event.data)

                # Load session-scoped variables (canonical store).
                # session_id may be "" for session-less events; session_variables.session_id
                # is a native uuid column, so skip the lookup instead of binding "".
                variables: dict[str, Any] = {}
                variable_load_failed = False
                if self._session_var_manager and session_id:
                    try:
                        variables = dict(
                            await asyncio.to_thread(
                                self._session_var_manager.get_variables,
                                session_id,
                            )
                        )
                    except _DATABASE_TIMEOUTS:
                        raise
                    except Exception as e:
                        if event.event_type == HookEventType.STOP:
                            logger.warning(
                                "Failed to load session variables on STOP - blocking for safety: %s",
                                e,
                                exc_info=True,
                            )
                            response = HookResponse(
                                decision="block",
                                reason="Could not load session state. Try again.",
                            )
                            await audit_source_block(
                                self,
                                event,
                                rule_id="variable-load-failure",
                                reason=response.reason or "",
                            )
                            return response
                        variable_load_failed = True
                        logger.debug(
                            "Could not load session variables for rules session=%s event=%s: %s",
                            session_id,
                            event.event_type,
                            e,
                            exc_info=True,
                        )

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
                            variables["current_step_status_message"] = (
                                step_context.status_message or ""
                            )
                            variables["current_step_description"] = step_context.description or ""
                        else:
                            variables["current_step"] = ""
                            variables["current_step_status_message"] = ""
                            variables["current_step_description"] = ""
                    except _DATABASE_TIMEOUTS:
                        raise
                    except Exception as e:
                        logger.warning(
                            "Could not inject current_step from workflow instance "
                            "session=%s event=%s: %s",
                            session_id,
                            event.event_type,
                            e,
                            exc_info=True,
                        )

                # Lazy-init variable presets for sessions that started before gobby init.
                # Mirrors the baseline_dirty_files pattern below — one-time DB hit per session.
                if "_variable_defaults_loaded" not in variables:
                    try:
                        from gobby.workflows.variable_defaults import (
                            merge_unloaded_variable_defaults,
                        )

                        defaults = await asyncio.to_thread(
                            merge_unloaded_variable_defaults,
                            self.rule_engine.db,
                            session_id,
                            variables,
                        )
                        if defaults:
                            variables.update(defaults)
                            if (
                                self._session_var_manager
                                and session_id
                                and not variable_load_failed
                            ):
                                await asyncio.to_thread(
                                    self._session_var_manager.merge_variables,
                                    session_id,
                                    defaults,
                                )
                    except _DATABASE_TIMEOUTS:
                        raise
                    except Exception as e:
                        logger.warning(
                            "Could not lazy-load variable defaults session=%s project=%s: %s",
                            session_id,
                            event.project_id,
                            e,
                            exc_info=True,
                        )

                from gobby.workflows.git_utils import resolve_git_worktree_root_async

                metadata_path = event.metadata.get("project_path")
                worktree_root = await resolve_git_worktree_root_async(
                    event.cwd, metadata_path if isinstance(metadata_path, str) else None
                )
                project_path = await asyncio.to_thread(
                    self._resolve_project_path, event, worktree_root
                )
                if not project_path:
                    message = (
                        f"_evaluate_rules: no project_path resolved for session={session_id} "
                        f"event={event.event_type} source={event.source} "
                        f"cwd={event.cwd!r} project_id={event.project_id!r} "
                        f"metadata_path={event.metadata.get('project_path')!r}"
                    )
                    event_data = event.data if isinstance(event.data, dict) else {}
                    has_candidate_path = bool(
                        event.cwd
                        or event.metadata.get("project_path")
                        or event_data.get("project_path")
                    )
                    if _is_known_no_repo_project(event.project_id) or has_candidate_path:
                        logger.debug(message)
                    else:
                        logger.warning(message)

                event_data = event.data if isinstance(event.data, dict) else {}
                # close_task verifies its own attributed paths inside the close
                # transaction, so its before_tool gate does not pre-judge them.
                skip_ledger_dirty_state = event.event_type == HookEventType.BEFORE_TOOL and (
                    _get_tool_identity(event_data) in {"close_task", "gobby-tasks:close_task"}
                )

                from gobby.workflows.ledger_reconcile import (
                    git_activity_detected,
                    reconcile_edit_ledgers,
                    session_dirty_file_set,
                )
                from gobby.workflows.task_claim_state import (
                    target_task_has_edits,
                    task_edited_file_set,
                )

                target_task_id = _target_task_id_for_event(event, variables)
                variables["target_task_has_edits"] = target_task_has_edits(
                    variables, target_task_id
                )

                eval_context: dict[str, Any] = {"foreign_dirty_edit_conflict": ""}
                if (
                    event.event_type == HookEventType.BEFORE_TOOL
                    and session_id
                    and event.project_id
                    and project_path
                ):
                    from gobby.workflows.commit_guard import (
                        foreign_dirty_edit_conflict,
                        foreign_staged_commit_conflict,
                    )

                    eval_context[
                        "foreign_staged_commit_conflict"
                    ] = await foreign_staged_commit_conflict(
                        self.rule_engine.db,
                        event,
                        session_id=session_id,
                        project_id=event.project_id,
                        project_path=project_path,
                    )
                    canonical_paths = event_data.get("canonical_file_paths") or event_data.get(
                        "canonical_file_path"
                    )
                    if event_data.get("canonical_repo_mutation") is True and canonical_paths:
                        eval_context[
                            "foreign_dirty_edit_conflict"
                        ] = await foreign_dirty_edit_conflict(
                            self.rule_engine.db,
                            event,
                            session_id=session_id,
                            project_id=event.project_id,
                            project_path=project_path,
                        )
                else:
                    eval_context["foreign_staged_commit_conflict"] = ""

                # Snapshot BEFORE observers to capture both observer and rule changes in the diff
                pre_eval = deepcopy(variables)

                # Run built-in observers BEFORE rule evaluation
                observer_failures = await asyncio.to_thread(
                    self._run_observers,
                    event,
                    session_id,
                    variables,
                )
                if (
                    event.event_type == HookEventType.STOP
                    and "reconcile_claimed_tasks" in observer_failures
                ):
                    response = HookResponse(
                        decision="block",
                        reason="Could not reconcile claimed tasks. Try again.",
                    )
                    await audit_source_block(
                        self,
                        event,
                        rule_id="reconciliation-failure",
                        reason=response.reason or "",
                        variables=variables,
                    )
                    return response

                # Git runs only after the session's own git activity, and only
                # over the ledger's paths, to release what it reports clean.
                if (
                    event.event_type == HookEventType.AFTER_TOOL
                    and session_id
                    and project_path
                    and self._session_var_manager
                    and not variable_load_failed
                    and git_activity_detected(event)
                ):
                    released = await reconcile_edit_ledgers(
                        variables,
                        session_id,
                        variable_manager=self._session_var_manager,
                        project_path=project_path,
                    )
                    if released:
                        logger.debug(
                            "Session %s released clean ledger paths: %s", session_id, released
                        )
                        variables["target_task_has_edits"] = target_task_has_edits(
                            variables, target_task_id
                        )

                # Dirty state is the daemon's edit ledger; no hook event asks git.
                eval_context["has_dirty_files"] = not skip_ledger_dirty_state and bool(
                    session_dirty_file_set(variables)
                )
                eval_context["target_task_has_edits"] = variables["target_task_has_edits"]
                eval_context["has_target_task_dirty_files"] = not skip_ledger_dirty_state and bool(
                    task_edited_file_set(variables, target_task_id)
                )

                eval_context["found_work_shirk"] = False
                eval_context["found_work_shirk_confirmed"] = False
                eval_context["terminal_validation_failure"] = False
                eval_context["terminal_validation_failure_commands"] = []
                eval_context["unclaimed_found_work"] = False
                eval_context["unclaimed_found_work_tasks"] = []
                if (
                    _is_turn_end_event(event.event_type)
                    and session_id
                    and not variables.get("plan_mode")
                    and not variables.get("is_spawned_agent")
                ):
                    facts = await self._found_work_analyzer.analyze(
                        event=event,
                        session_id=session_id,
                        variables=variables,
                        project_path=project_path,
                    )
                    eval_context["found_work_shirk"] = facts.shirk
                    eval_context["found_work_shirk_confirmed"] = facts.shirk_confirmed
                    eval_context["terminal_validation_failure"] = bool(
                        facts.terminal_validation_failures
                    )
                    eval_context["terminal_validation_failure_commands"] = list(
                        facts.terminal_validation_failures
                    )

                if _is_turn_end_event(event.event_type) and session_id:
                    deferred = {
                        ref
                        for ref in (variables.get("_found_work_deferred_tasks") or ())
                        if isinstance(ref, str)
                    }
                    unclaimed_tasks = await asyncio.to_thread(
                        self._found_work_analyzer.unclaimed_found_work,
                        session_id,
                        armed_at=variables.get(FOUND_WORK_GATE_ARMED_AT_VARIABLE),
                        deferred=frozenset(deferred),
                    )
                    prompt = str(variables.get("_current_user_prompt") or "")
                    if unclaimed_tasks and is_found_work_deferral(prompt):
                        # A deferral names the refs open when the user gave it, so
                        # latch those into session state. Re-reading the live prompt
                        # would revoke it at the next compaction, whose continuation
                        # turn carries harness text instead of the user's words.
                        deferred.update(unclaimed_tasks)
                        variables["_found_work_deferred_tasks"] = sorted(deferred)
                        unclaimed_tasks = ()
                    eval_context["unclaimed_found_work"] = bool(unclaimed_tasks)
                    eval_context["unclaimed_found_work_tasks"] = list(unclaimed_tasks)

                response = await self.rule_engine.evaluate(
                    event=event,
                    session_id=session_id,
                    variables=variables,
                    eval_context=eval_context,
                    blocking_deadline=blocking_deadline,
                )

                staged_payload = response.metadata.get(STAGED_EFFECTS_FIELD)
                staged_keys: set[str] = set()
                if isinstance(staged_payload, dict):
                    record_worker_staging(staged_payload)
                    updates = staged_payload.get("session_variables")
                    if isinstance(updates, dict):
                        staged_keys = {key for key in updates if isinstance(key, str)}

                # Persist all variables changed by observers OR rule effects.
                # Skip when session state could not be loaded or session_id is "".
                # on_receipt mutations stay in the receipt until acknowledgment.
                if self._session_var_manager and session_id and not variable_load_failed:
                    changed = {
                        k: v
                        for k, v in variables.items()
                        if k not in staged_keys and (k not in pre_eval or pre_eval[k] != v)
                    }
                    if changed:
                        await asyncio.to_thread(
                            self._session_var_manager.merge_variables,
                            session_id,
                            changed,
                        )

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
        except (DatabaseOperationDeadlineExceeded, QueryCanceled):
            raise
        except Exception as e:
            logger.exception("RuleEngine evaluation failed: %s", e)
            raise

    async def evaluate_async(
        self,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        """Evaluate rules asynchronously for callers that already own the loop."""
        enabled, timeout = self._resolve_policy()
        if not enabled:
            return HookResponse(decision="allow")
        if timeout is None:
            return await self._evaluate_rules(event, blocking_deadline=blocking_deadline)

        try:
            with database_operation_deadline(timeout_seconds=timeout):
                return await asyncio.wait_for(
                    self._evaluate_rules(event, blocking_deadline=blocking_deadline),
                    timeout=timeout,
                )
        except (TimeoutError, *_DATABASE_TIMEOUTS) as exc:
            session_id = event.metadata.get("_platform_session_id") or ""
            raise WorkflowEvaluationTimeout(
                event_type=event.event_type,
                session_id=session_id,
                timeout_seconds=timeout,
            ) from exc

    def evaluate(
        self,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        """Evaluate rules for a hook event.

        Primary entry point for workflow evaluation.
        """
        enabled, timeout = self._resolve_policy()
        if not enabled:
            return HookResponse(decision="allow")
        # ``evaluate_async`` owns the evaluation deadline; this only bounds the
        # wait so a wedged runtime cannot hold this adapter thread forever.
        runtime_wait = None if timeout is None else timeout + _RUNTIME_WAIT_MARGIN_SECONDS
        try:
            try:
                asyncio.get_running_loop()
                logger.warning("Could not run workflow engine: Event loop is already running.")
                return HookResponse(decision="allow")
            except RuntimeError:
                if self._evaluation_runtime is None:
                    raise RuntimeError(
                        "Synchronous workflow evaluation requires a runtime"
                    ) from None
                response = self._evaluation_runtime.run(
                    self.evaluate_async(event, blocking_deadline=blocking_deadline),
                    timeout=runtime_wait,
                )
                # The runtime evaluates on its own "gobby-workflow-runtime"
                # thread while ``take_worker_staging`` runs on this adapter
                # thread, so the staged payload travels back in the response and
                # is re-recorded here. Without it the receipt is prepared empty
                # and every on_receipt ``acknowledge_variable`` is lost (#21424).
                # Re-recording is an idempotent merge, so it stays correct when
                # the staging buffer already crossed the hop by context.
                staged = response.metadata.get(STAGED_EFFECTS_FIELD)
                if isinstance(staged, dict) and staged:
                    record_worker_staging(staged)
                return response

        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            return self._handle_cancelled(event)
        except WorkflowEvaluationTimeout:
            raise
        except TimeoutError as exc:
            # The runtime never scheduled the coroutine, so it raised nothing of
            # its own. Report it as an evaluation timeout so the hook boundary
            # degrades it the same way, with the same diagnostics.
            raise WorkflowEvaluationTimeout(
                event_type=event.event_type,
                session_id=event.metadata.get("_platform_session_id") or "",
                timeout_seconds=runtime_wait if runtime_wait is not None else 0.0,
            ) from exc
        except Exception as e:
            logger.exception("Error evaluating rules: %s: %s", type(e).__name__, e)
            raise

    def shutdown(self) -> None:
        """Shut down the isolated evaluation runtime, if configured."""
        if self._evaluation_runtime is not None:
            self._evaluation_runtime.shutdown()

    def handle(
        self,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        """Handle a hook event by evaluating declarative rules."""
        return self.evaluate(event, blocking_deadline=blocking_deadline)
