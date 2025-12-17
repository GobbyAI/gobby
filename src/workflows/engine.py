import logging
from datetime import UTC, datetime

from gobby.hooks.events import HookEvent, HookResponse

from .definitions import WorkflowDefinition, WorkflowState
from .evaluator import ConditionEvaluator
from .loader import WorkflowLoader
from .state_manager import WorkflowStateManager

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .actions import ActionExecutor

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """
    Core engine for executing phase-based workflows.
    """

    def __init__(
        self,
        loader: WorkflowLoader,
        state_manager: WorkflowStateManager,
        action_executor: "ActionExecutor",
        evaluator: ConditionEvaluator | None = None,
    ):
        self.loader = loader
        self.state_manager = state_manager
        self.action_executor = action_executor
        self.evaluator = evaluator or ConditionEvaluator()

    async def handle_event(self, event: HookEvent) -> HookResponse:
        """
        Main entry point for hook events.
        """
        session_id = event.metadata.get("_platform_session_id")
        if not session_id:
            return HookResponse(decision="allow")  # No session, no workflow

        # 1. Load state
        state = self.state_manager.get_state(session_id)

        # 2. If no state, check triggers to start one (e.g. on_session_start)
        # Note: This logic might need to move to a specialized trigger handler
        # For now, simplistic check

        if not state:
            # TODO: Logic to load workflow?
            # For now, return allow
            return HookResponse(decision="allow")

        # Stuck prevention: Check if phase duration exceeding limit
        # This is a basic implementation of "Stuck Detection"
        if state.phase_entered_at:
            duration = (datetime.now(UTC) - state.phase_entered_at).total_seconds()
            # Hardcoded limit for MVP: 30 minutes
            if duration > 1800:
                # Force transition to reflect if not already there
                if state.phase != "reflect":
                    workflow = self.loader.load_workflow(state.workflow_name)
                    if workflow and workflow.get_phase("reflect"):
                        await self.transition_to(state, "reflect", workflow)
                        return HookResponse(
                            decision="modify",
                            context="[System Alert] Phase duration limit exceeded. Transitioning to 'reflect' phase.",
                        )

        # 3. Load definition
        workflow = self.loader.load_workflow(state.workflow_name)
        if not workflow:
            logger.error(f"Workflow '{state.workflow_name}' not found for session {session_id}")
            return HookResponse(decision="allow")

        # 4. Process event
        # Logic matches WORKFLOWS.md "Evaluation Flow"

        # Determine context for evaluation
        eval_context = {
            "event": event,
            "workflow_state": state,
            "session": {},  # TODO: Attach session info
            "tool_name": getattr(event, "tool_name", None),
            "tool_args": getattr(event, "tool_args", {}),
        }

        current_phase = workflow.get_phase(state.phase)
        if not current_phase:
            logger.error(f"Phase '{state.phase}' not found in workflow '{workflow.name}'")
            return HookResponse(decision="allow")

        # Check blocked tools
        if event.event_type.value == "tool_call":  # Adjust enum check
            tool_name = eval_context["tool_name"]

            # Check blocked list
            if tool_name in current_phase.blocked_tools:
                return HookResponse(
                    decision="block",
                    reason=f"Tool '{tool_name}' is blocked in phase '{state.phase}'.",
                )

            # Check allowed list (if not "all")
            if current_phase.allowed_tools != "all":
                if tool_name not in current_phase.allowed_tools:
                    return HookResponse(
                        decision="block",
                        reason=f"Tool '{tool_name}' is not in allowed list for phase '{state.phase}'.",
                    )

            # Check rules
            for rule in current_phase.rules:
                if self.evaluator.evaluate(rule.when, eval_context):
                    if rule.action == "block":
                        return HookResponse(
                            decision="block", reason=rule.message or "Blocked by workflow rule."
                        )
                    # Handle other actions like warn, require_approval

        # Check transitions
        for transition in current_phase.transitions:
            if self.evaluator.evaluate(transition.when, eval_context):
                # Transition!
                await self.transition_to(state, transition.to, workflow)
                return HookResponse(
                    decision="modify", context=f"Transitioning to phase: {transition.to}"
                )

        # Check exit conditions
        if self.evaluator.check_exit_conditions(current_phase.exit_conditions, state):
            # TODO: Determine next phase or completion logic
            # For now, simplistic 'next phase' if linear, or rely on transitions
            # WORKFLOWS.md says: next_phase = workflow.get_next_phase(state.phase)
            pass

        # Update stats (generic)
        if event.event_type.value == "tool_result":  # Adjust enum value
            state.phase_action_count += 1
            state.total_action_count += 1
            self.state_manager.save_state(state)  # Persist updates

        return HookResponse(decision="allow")

    async def transition_to(
        self, state: WorkflowState, new_phase_name: str, workflow: WorkflowDefinition
    ) -> None:
        """
        Execute transition logic.
        """
        old_phase = workflow.get_phase(state.phase)
        new_phase = workflow.get_phase(new_phase_name)

        if not new_phase:
            logger.error(f"Cannot transition to unknown phase '{new_phase_name}'")
            return

        logger.info(
            f"Transitioning session {state.session_id} from '{state.phase}' to '{new_phase_name}'"
        )

        # Execute on_exit of old phase
        if old_phase:
            await self._execute_actions(old_phase.on_exit, state)

        # Update state
        state.phase = new_phase_name
        state.phase_entered_at = datetime.now(UTC)
        state.phase_action_count = 0
        state.context_injected = False  # Reset for new phase context

        self.state_manager.save_state(state)

        # Execute on_enter of new phase
        await self._execute_actions(new_phase.on_enter, state)

    async def _execute_actions(self, actions: list[dict], state: WorkflowState) -> None:
        """
        Execute a list of actions.
        """
        from .actions import ActionContext

        context = ActionContext(
            session_id=state.session_id,
            state=state,
            db=self.action_executor.db,
            session_manager=self.action_executor.session_manager,
        )

        for action_def in actions:
            action_type = action_def.get("action")
            if not action_type:
                continue

            result = await self.action_executor.execute(action_type, context, **action_def)

            if result and "inject_context" in result:
                # Log context injection for now
                logger.info(f"Context injected: {result['inject_context'][:50]}...")
