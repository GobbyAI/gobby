import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookResponse

from .definitions import WorkflowDefinition, WorkflowState
from .evaluator import ConditionEvaluator
from .loader import WorkflowLoader
from .state_manager import WorkflowStateManager

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

    # Maps canonical trigger names to their legacy aliases for backward compatibility.
    TRIGGER_ALIASES: dict[str, list[str]] = {
        "on_before_agent": ["on_prompt_submit"],
        "on_before_tool": ["on_tool_call"],
        "on_after_tool": ["on_tool_result"],
    }

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
            template_engine=self.action_executor.template_engine,
            llm_service=self.action_executor.llm_service,
            transcript_processor=self.action_executor.transcript_processor,
            config=self.action_executor.config,
        )

        for action_def in actions:
            action_type = action_def.get("action")
            if not action_type:
                continue

            result = await self.action_executor.execute(action_type, context, **action_def)

            if result and "inject_context" in result:
                # Log context injection for now
                logger.info(f"Context injected: {result['inject_context'][:50]}...")

    # Maximum iterations to prevent infinite loops
    MAX_TRIGGER_ITERATIONS = 10

    async def evaluate_all_lifecycle_workflows(
        self, event: HookEvent, context_data: dict[str, Any] | None = None
    ) -> HookResponse:
        """
        Discover and evaluate all lifecycle workflows for the given event.

        Workflows are evaluated in order (project first by priority/alpha, then global).
        Loops until no more triggers fire (up to MAX_TRIGGER_ITERATIONS).

        Args:
            event: The hook event to evaluate
            context_data: Optional context data passed between actions

        Returns:
            Merged HookResponse with combined context and first non-allow decision.
        """
        project_path = event.data.get("cwd") if event.data else None

        # Discover all lifecycle workflows
        workflows = self.loader.discover_lifecycle_workflows(project_path)

        if not workflows:
            logger.debug("No lifecycle workflows discovered")
            return HookResponse(decision="allow")

        logger.debug(
            f"Discovered {len(workflows)} lifecycle workflow(s): "
            f"{[w.name for w in workflows]}"
        )

        # Accumulate context from all workflows
        all_context: list[str] = []
        final_decision = "allow"
        final_reason: str | None = None

        # Initialize shared context for chaining between workflows
        if context_data is None:
            context_data = {}

        # Loop until no triggers fire (or max iterations)
        for iteration in range(self.MAX_TRIGGER_ITERATIONS):
            triggers_fired = False

            for discovered in workflows:
                workflow = discovered.definition

                response = await self._evaluate_workflow_triggers(
                    workflow, event, context_data
                )

                # Accumulate context
                if response.context:
                    all_context.append(response.context)
                    triggers_fired = True

                # First non-allow decision wins
                if response.decision != "allow" and final_decision == "allow":
                    final_decision = response.decision
                    final_reason = response.reason

                # If blocked, stop immediately
                if response.decision == "block":
                    logger.info(
                        f"Workflow '{discovered.name}' blocked event: {response.reason}"
                    )
                    return HookResponse(
                        decision="block",
                        reason=response.reason,
                        context="\n\n".join(all_context) if all_context else None,
                    )

            # If no triggers fired this iteration, we're done
            if not triggers_fired:
                logger.debug(f"No triggers fired in iteration {iteration + 1}, stopping")
                break

            logger.debug(f"Triggers fired in iteration {iteration + 1}, continuing")

        return HookResponse(
            decision=final_decision,
            reason=final_reason,
            context="\n\n".join(all_context) if all_context else None,
        )

    async def _evaluate_workflow_triggers(
        self,
        workflow: "WorkflowDefinition",
        event: HookEvent,
        context_data: dict[str, Any],
    ) -> HookResponse:
        """
        Evaluate triggers for a single workflow definition.

        Args:
            workflow: The workflow definition to evaluate
            event: The hook event
            context_data: Shared context for chaining (mutated by actions)

        Returns:
            HookResponse from this workflow's triggers
        """
        from .definitions import WorkflowState

        # Map hook event to trigger name
        trigger_name = f"on_{event.event_type.name.lower()}"

        # Look up triggers - try canonical name first, then aliases
        triggers = []
        if workflow.triggers:
            triggers = workflow.triggers.get(trigger_name, [])
            if not triggers:
                aliases = self.TRIGGER_ALIASES.get(trigger_name, [])
                for alias in aliases:
                    triggers = workflow.triggers.get(alias, [])
                    if triggers:
                        break

        if not triggers:
            return HookResponse(decision="allow")

        logger.debug(
            f"Evaluating {len(triggers)} trigger(s) for '{trigger_name}' "
            f"in workflow '{workflow.name}'"
        )

        # Create ephemeral state for action execution
        from .actions import ActionContext

        session_id = event.metadata.get("_platform_session_id") or "global"

        state = WorkflowState(
            session_id=session_id,
            workflow_name=workflow.name,
            phase="global",
            phase_entered_at=datetime.now(UTC),
            phase_action_count=0,
            total_action_count=0,
            artifacts=event.data.get("artifacts", {}) if event.data else {},
            observations=[],
            reflection_pending=False,
            context_injected=False,
            variables=context_data,
            task_list=None,
            current_task_index=0,
            files_modified_this_task=0,
        )

        action_ctx = ActionContext(
            session_id=session_id,
            state=state,
            db=self.action_executor.db,
            session_manager=self.action_executor.session_manager,
            template_engine=self.action_executor.template_engine,
            llm_service=self.action_executor.llm_service,
            transcript_processor=self.action_executor.transcript_processor,
            config=self.action_executor.config,
        )

        injected_context: list[str] = []

        for trigger in triggers:
            # Check 'when' condition if present
            when_condition = trigger.get("when")
            if when_condition:
                eval_ctx = {"event": event, "workflow_state": state, "handoff": context_data}
                eval_ctx.update(context_data)
                if not self.evaluator.evaluate(when_condition, eval_ctx):
                    continue

            # Execute action
            action_type = trigger.get("action")
            if not action_type:
                continue

            logger.debug(f"Executing action '{action_type}' in workflow '{workflow.name}'")
            try:
                kwargs = trigger.copy()
                kwargs.pop("action", None)
                kwargs.pop("when", None)

                result = await self.action_executor.execute(action_type, action_ctx, **kwargs)

                if result and isinstance(result, dict):
                    # Update shared context for chaining
                    context_data.update(result)
                    state.variables.update(result)

                    if "inject_context" in result:
                        injected_context.append(result["inject_context"])

            except Exception as e:
                logger.error(
                    f"Failed to execute action '{action_type}' in '{workflow.name}': {e}",
                    exc_info=True,
                )

        return HookResponse(
            decision="allow",
            context="\n\n".join(injected_context) if injected_context else None,
        )

    async def evaluate_lifecycle_triggers(
        self, workflow_name: str, event: HookEvent, context_data: dict[str, Any] | None = None
    ) -> HookResponse:
        """
        Evaluate triggers for a specific lifecycle workflow (e.g. session-handoff).
        Does not require an active session state.
        """
        # Get project path from event for project-specific workflow lookup
        project_path = event.data.get("cwd") if event.data else None
        logger.debug(f"evaluate_lifecycle_triggers: workflow={workflow_name}, project_path={project_path}")

        workflow = self.loader.load_workflow(workflow_name, project_path=project_path)
        if not workflow:
            logger.warning(f"Workflow '{workflow_name}' not found in project_path={project_path}")
            return HookResponse(decision="allow")

        logger.debug(f"Workflow '{workflow_name}' loaded, triggers={list(workflow.triggers.keys()) if workflow.triggers else []}")

        # Map hook event to trigger name (canonical name based on HookEventType)
        trigger_name = f"on_{event.event_type.name.lower()}"  # e.g. on_session_start, on_before_agent

        # Look up triggers - try canonical name first, then aliases
        triggers = []
        if workflow.triggers:
            triggers = workflow.triggers.get(trigger_name, [])
            # If no triggers found, check aliases (e.g., on_prompt_submit for on_before_agent)
            if not triggers:
                aliases = self.TRIGGER_ALIASES.get(trigger_name, [])
                for alias in aliases:
                    triggers = workflow.triggers.get(alias, [])
                    if triggers:
                        logger.debug(f"Using alias '{alias}' for trigger '{trigger_name}'")
                        break

        if not triggers:
            logger.debug(f"No triggers for '{trigger_name}' in workflow '{workflow_name}'")
            return HookResponse(decision="allow")

        logger.info(f"Executing lifecycle triggers for '{workflow_name}' on '{trigger_name}', count={len(triggers)}")

        # Create a temporary/ephemeral context for execution
        from .actions import ActionContext
        from .definitions import WorkflowState

        # Create a dummy state for context - lifecycle workflows shouldn't depend on phase state
        # but actions might need access to 'state.artifacts' or similar if provided
        session_id = event.metadata.get("_platform_session_id") or "global"

        state = WorkflowState(
            session_id=session_id,
            workflow_name=workflow_name,
            phase="global",
            phase_entered_at=datetime.now(UTC),
            phase_action_count=0,
            total_action_count=0,
            artifacts=event.data.get("artifacts", {}),  # Pass artifacts if available
            observations=[],
            reflection_pending=False,
            context_injected=False,
            variables=context_data or {},  # Pass extra context as variables
            task_list=None,
            current_task_index=0,
            files_modified_this_task=0,
        )

        action_ctx = ActionContext(
            session_id=session_id,
            state=state,
            db=self.action_executor.db,
            session_manager=self.action_executor.session_manager,
            template_engine=self.action_executor.template_engine,
            llm_service=self.action_executor.llm_service,
            transcript_processor=self.action_executor.transcript_processor,
            config=self.action_executor.config,
        )

        injected_context = []

        for trigger in triggers:
            # Check 'when' condition if present
            when_condition = trigger.get("when")
            if when_condition:
                # Simple eval context
                eval_ctx = {"event": event, "workflow_state": state, "handoff": context_data or {}}
                if context_data:
                    eval_ctx.update(context_data)
                eval_result = self.evaluator.evaluate(when_condition, eval_ctx)
                logger.debug(f"When condition '{when_condition}' evaluated to {eval_result}, event.data.reason={event.data.get('reason') if event.data else None}")
                if not eval_result:
                    continue

            # Execute action
            action_type = trigger.get("action")
            if not action_type:
                continue

            logger.info(f"Executing action '{action_type}' for trigger")
            try:
                # Pass triggers definition as kwargs
                kwargs = trigger.copy()
                kwargs.pop("action", None)
                kwargs.pop("when", None)

                result = await self.action_executor.execute(action_type, action_ctx, **kwargs)
                logger.debug(f"Action '{action_type}' returned: {type(result).__name__}, keys={list(result.keys()) if isinstance(result, dict) else 'N/A'}")

                if result:
                    # Update context for subsequent actions
                    if isinstance(result, dict):
                        if context_data is None:
                            context_data = {}
                        context_data.update(result)
                        state.variables.update(result)

                    if "inject_context" in result:
                        logger.debug(f"Found inject_context in result, length={len(result['inject_context'])}")
                        injected_context.append(result["inject_context"])

            except Exception as e:
                logger.error(
                    f"Failed to execute lifecycle action '{action_type}': {e}", exc_info=True
                )

        response = HookResponse(decision="allow")
        if injected_context:
            response.context = "\n\n".join(injected_context)

        return response
