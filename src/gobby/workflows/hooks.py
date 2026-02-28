import asyncio
import concurrent.futures
import logging
import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse

if TYPE_CHECKING:
    from .engine import WorkflowEngine
    from .rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class WorkflowHookHandler:
    """
    Integrates WorkflowEngine into the HookManager.
    Wraps the async engine to be callable from synchronous hooks.

    Uses RuleEngine for declarative rule evaluation on hook events.
    """

    def __init__(
        self,
        engine: "WorkflowEngine",
        loop: asyncio.AbstractEventLoop | None = None,
        timeout: float = 30.0,  # Timeout for workflow operations in seconds
        enabled: bool = True,
        rule_engine: "RuleEngine | None" = None,
    ):
        self.engine = engine
        self.rule_engine = rule_engine
        self._loop = loop
        # Convert 0 to None for asyncio (0 means no timeout)
        self.timeout = timeout if timeout > 0 else None
        self._enabled = enabled

        # Session variable manager for persisting rule set_variable effects
        self._session_var_manager = None
        if rule_engine:
            from gobby.workflows.state_manager import SessionVariableManager

            self._session_var_manager = SessionVariableManager(rule_engine.db)

        # If no loop provided, try to get one or create one for this thread
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

    async def _evaluate_rules(self, event: HookEvent) -> HookResponse:
        """Evaluate rules for a hook event using the RuleEngine.

        Loads variables from both workflow_states and session_variables tables,
        merging them (session_variables take precedence). After evaluation,
        persists any variables changed by rule set_variable effects back to
        session_variables so they survive across evaluations.

        Returns allow if no rule engine is configured.
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

            # Snapshot before evaluation to detect changes from set_variable effects
            pre_eval = deepcopy(variables)

            response = await self.rule_engine.evaluate(
                event=event,
                session_id=session_id,
                variables=variables,
            )

            # Persist variables changed by rule effects to session_variables
            if self._session_var_manager:
                changed = {
                    k: v for k, v in variables.items() if k not in pre_eval or pre_eval[k] != v
                }
                if changed:
                    self._session_var_manager.merge_variables(session_id, changed)

            return response
        except Exception as e:
            logger.error(f"RuleEngine evaluation failed: {e}", exc_info=True)
            raise  # Propagate — don't swallow into a fake "allow"

    def evaluate(self, event: HookEvent) -> HookResponse:
        """Evaluate rules for a hook event.

        Uses the RuleEngine for declarative rule evaluation. This is the
        primary entry point for workflow evaluation.

        Args:
            event: The hook event to handle

        Returns:
            HookResponse from rule engine evaluation
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
                # If we get here, a loop is running
                logger.warning("Could not run workflow engine: Event loop is already running.")
                return HookResponse(decision="allow")
            except RuntimeError:
                # No loop running, safe to use asyncio.run
                return asyncio.run(self._evaluate_rules(event))

        except concurrent.futures.CancelledError:
            return self._handle_cancelled(event)
        except Exception as e:
            logger.error(f"Error evaluating rules: {e}", exc_info=True)
            raise  # Propagate to _evaluate_workflow_rules for hook-manager.log visibility

    def handle(self, event: HookEvent) -> HookResponse:
        """Handle a hook event by evaluating declarative rules.

        Returns the rule engine response directly so that metadata
        (including mcp_call effects) is preserved for the caller.
        """
        return self.evaluate(event)

    def activate_workflow(
        self,
        workflow_name: str,
        session_id: str,
        project_path: str | None = None,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Activate a step-based workflow for a session.

        This is used during session startup for terminal-mode agents that have
        a workflow_name set. It's a synchronous wrapper around the engine's
        activate_workflow method.

        Args:
            workflow_name: Name of the workflow to activate
            session_id: Session ID to activate for
            project_path: Optional project path for workflow discovery
            variables: Optional initial variables to merge with workflow defaults

        Returns:
            Dict with success status and workflow info
        """
        if not self._enabled:
            return {"success": False, "error": "Workflow engine is disabled"}

        from pathlib import Path

        path = Path(project_path) if project_path else None

        try:
            if self._loop and self._loop.is_running():
                if threading.current_thread() is threading.main_thread():
                    return {"success": False, "error": "Event loop conflict"}
                future = asyncio.run_coroutine_threadsafe(
                    self.engine.activate_workflow(
                        workflow_name=workflow_name,
                        session_id=session_id,
                        project_path=path,
                        variables=variables,
                    ),
                    self._loop,
                )
                return future.result(timeout=self.timeout)

            try:
                asyncio.get_running_loop()
                return {"success": False, "error": "Event loop conflict"}
            except RuntimeError:
                return asyncio.run(
                    self.engine.activate_workflow(
                        workflow_name=workflow_name,
                        session_id=session_id,
                        project_path=path,
                        variables=variables,
                    )
                )
        except concurrent.futures.CancelledError:
            logger.warning("Workflow activation cancelled")
            return {"success": False, "error": "Workflow activation was cancelled"}
        except Exception as e:
            logger.error(f"Error activating workflow: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
