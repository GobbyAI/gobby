import asyncio
import concurrent.futures
import logging
import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.session_tasks import SessionTaskManager

    from .rule_engine import RuleEngine

logger = logging.getLogger(__name__)


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

    def _handle_cancelled(self, event: HookEvent) -> HookResponse:
        """Handle CancelledError by logging and returning appropriate response."""
        logger.warning("Workflow evaluation cancelled for %s", event.event_type)
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
            detect_commit_link,
            detect_mcp_call,
            detect_plan_mode_from_context,
            detect_task_claim,
            reconcile_claimed_tasks,
        )

        # Reconcile stale claimed_tasks on STOP before rule evaluation
        if event.event_type == HookEventType.STOP:
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
            detect_mcp_call(event, variables, session_id)

        # Plan mode detection (BEFORE_AGENT for system reminder tags)
        if event.event_type == HookEventType.BEFORE_AGENT:
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

            # Load session-scoped variables (canonical store)
            variables: dict[str, Any] = {}
            if self._session_var_manager:
                try:
                    variables = dict(self._session_var_manager.get_variables(session_id))
                except Exception as e:
                    logger.debug(f"Could not load session variables for rules: {e}")

            # Snapshot BEFORE observers to capture both observer and rule changes in the diff
            pre_eval = deepcopy(variables)

            # Run built-in observers BEFORE rule evaluation
            self._run_observers(event, session_id, variables)

            response = await self.rule_engine.evaluate(
                event=event,
                session_id=session_id,
                variables=variables,
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
