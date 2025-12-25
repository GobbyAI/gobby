import logging
from typing import Any

from .definitions import WorkflowState

logger = logging.getLogger(__name__)


class ConditionEvaluator:
    """
    Evaluates 'when' conditions in workflows.
    Supports simple boolean logic and variable access.
    """

    def evaluate(self, condition: str, context: dict[str, Any]) -> bool:
        """
        Evaluate a condition string against a context dictionary.

        Args:
            condition: The condition string (e.g., "phase_action_count > 5")
            context: Dictionary containing Available variables (state, event, etc.)

        Returns:
            Boolean result of the evaluation.
        """
        if not condition:
            return True

        try:
            # SAFETY: Using eval() is risky but standard for this type of flexibility until
            # we implement a proper expression parser. We restrict globals to builtins logic.
            # In a production environment, we should use a safer parser like `simpleeval` or `jinja2`.
            # For this MVP, we rely on the context being controlled.

            # Simple sanitization/safety check could go here

            # Allow common helpers
            allowed_globals = {
                "__builtins__": {},
                "len": len,
                "bool": bool,
                "str": str,
                "int": int,
                "list": list,
                "dict": dict,
            }

            return bool(eval(condition, allowed_globals, context))
        except Exception as e:
            logger.warning(f"Condition evaluation failed: '{condition}'. Error: {e}")
            return False

    def check_exit_conditions(self, conditions: list[dict[str, Any]], state: WorkflowState) -> bool:
        """
        Check if all exit conditions are met. (AND logic)
        """
        context = {
            "workflow_state": state,
            "state": state,  # alias
            # Flatten state for easier access
            "phase_action_count": state.phase_action_count,
            "total_action_count": state.total_action_count,
            "variables": state.variables,
            "task_list": state.task_list,
        }

        for condition in conditions:
            cond_type = condition.get("type")

            if cond_type == "variable_set":
                var_name = condition.get("variable")
                if not var_name or var_name not in state.variables:
                    return False

            elif cond_type == "user_approval":
                # User approval is special - handled by the engine via hook response
                # But for pure logic checking, if we strictly check 'is approved', we check state
                # Here we might need to check a variable that indicates approval occurred
                pass  # TODO: Implement approval logic connection

            elif cond_type == "expression":
                expr = condition.get("expression")
                if expr and not self.evaluate(expr, context):
                    return False

            # Add other types as needed

        return True
