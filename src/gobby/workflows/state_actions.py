"""Workflow state management actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle workflow state persistence and variable management.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_workflow_state(db: Any, session_id: str, state: Any) -> dict[str, Any]:
    """Load workflow state from DB into the provided state object.

    Args:
        db: Database instance
        session_id: Session ID to load state for
        state: WorkflowState object to update

    Returns:
        Dict with state_loaded boolean
    """
    from gobby.workflows.state_manager import WorkflowStateManager

    state_manager = WorkflowStateManager(db)
    loaded_state = state_manager.get_state(session_id)

    if loaded_state:
        # Copy attributes from loaded state to existing state object
        for field in loaded_state.model_fields:
            val = getattr(loaded_state, field)
            setattr(state, field, val)
        return {"state_loaded": True}

    return {"state_loaded": False}


def save_workflow_state(db: Any, state: Any) -> dict[str, Any]:
    """Save workflow state to DB.

    Args:
        db: Database instance
        state: WorkflowState object to save

    Returns:
        Dict with state_saved boolean
    """
    from gobby.workflows.state_manager import WorkflowStateManager

    state_manager = WorkflowStateManager(db)
    state_manager.save_state(state)
    return {"state_saved": True}


def set_variable(state: Any, name: str | None, value: Any) -> dict[str, Any] | None:
    """Set a workflow variable.

    Args:
        state: WorkflowState object
        name: Variable name
        value: Variable value

    Returns:
        Dict with variable_set and value, or None if no name
    """
    if not name:
        return None

    if not state.variables:
        state.variables = {}

    state.variables[name] = value
    return {"variable_set": name, "value": value}


def increment_variable(
    state: Any, name: str | None, amount: int | float = 1
) -> dict[str, Any] | None:
    """Increment a numeric workflow variable.

    Args:
        state: WorkflowState object
        name: Variable name
        amount: Amount to increment by (default: 1)

    Returns:
        Dict with variable_incremented and value, or None if no name
    """
    if not name:
        return None

    if not state.variables:
        state.variables = {}

    current = state.variables.get(name, 0)
    if not isinstance(current, (int, float)):
        logger.error(
            f"increment_variable: Variable '{name}' is not numeric, got {type(current).__name__}: {current}"
        )
        raise TypeError(
            f"Cannot increment non-numeric variable '{name}': "
            f"expected int or float, got {type(current).__name__} with value {current!r}"
        )

    new_value = current + amount
    state.variables[name] = new_value
    return {"variable_incremented": name, "value": new_value}


def mark_loop_complete(state: Any) -> dict[str, Any]:
    """Mark the autonomous loop as complete.

    Args:
        state: WorkflowState object

    Returns:
        Dict with loop_marked_complete boolean
    """
    if not state.variables:
        state.variables = {}
    state.variables["stop_reason"] = "completed"
    return {"loop_marked_complete": True}


# --- ActionHandler-compatible wrappers ---
# These match the ActionHandler protocol: (context: ActionContext, **kwargs) -> dict | None


async def handle_load_workflow_state(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for load_workflow_state."""
    return await asyncio.to_thread(
        load_workflow_state, context.db, context.session_id, context.state
    )


async def handle_save_workflow_state(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for save_workflow_state."""
    return await asyncio.to_thread(save_workflow_state, context.db, context.state)


def _coerce_rendered_value(value: str) -> Any:
    """Coerce a template-rendered string back to a native Python type.

    Jinja2 render() always returns str, but workflow variables need int/float/bool/None
    to work correctly in transition conditions (e.g., ``wait_retry_count < 3``).
    """
    stripped = value.strip()
    if stripped.lower() in ("true", "false"):
        return stripped.lower() == "true"
    if stripped.lower() in ("null", "none", ""):
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return value


def _resolve_variable_name(kwargs: dict[str, Any], caller: str = "unknown") -> str | None:
    """Resolve variable name from 'name' or 'variable' kwargs with conflict warning."""
    name_val = kwargs.get("name")
    variable_val = kwargs.get("variable")
    if name_val and variable_val and name_val != variable_val:
        logger.warning(
            "%s: both 'name' (%s) and 'variable' (%s) provided with different values; "
            "using 'name' for backwards compatibility",
            caller,
            name_val,
            variable_val,
        )
    return name_val or variable_val


async def handle_set_variable(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for set_variable.

    Values containing Jinja2 templates ({{ ... }}) are rendered before setting.

    The variable name is resolved from kwargs using ``kwargs.get("name")`` first,
    falling back to ``kwargs.get("variable")``.  "name" takes precedence over
    "variable" for backwards compatibility.  If both keys are present with
    different truthy values, a warning is logged to alert callers to the
    potential conflict.
    """
    value = kwargs.get("value")

    # Render template if value contains Jinja2 syntax
    if isinstance(value, str) and "{{" in value:
        template_context = {
            "variables": context.state.variables or {},
            "state": context.state,
        }
        if context.template_engine:
            value = context.template_engine.render(value, template_context)
            # Jinja2 render() always returns str. Coerce back to native types
            # so workflow variables preserve int/float/bool/None semantics.
            value = _coerce_rendered_value(value)
        else:
            logger.warning("handle_set_variable: template_engine is None, skipping template render")

    resolved_name = _resolve_variable_name(kwargs, "handle_set_variable")
    return set_variable(context.state, resolved_name, value)


async def handle_increment_variable(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for increment_variable."""
    resolved_name = _resolve_variable_name(kwargs, "handle_increment_variable")
    return increment_variable(context.state, resolved_name, kwargs.get("amount", 1))


async def handle_mark_loop_complete(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for mark_loop_complete."""
    return mark_loop_complete(context.state)


async def handle_end_workflow(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """End the active workflow by disabling it.

    Sets enabled=false on the workflow instance so the engine stops evaluating it.
    Preserves state and variables for inspection.
    """
    from gobby.workflows.state_manager import WorkflowInstanceManager

    session_id = context.session_id
    workflow_name = context.state.workflow_name

    # Disable the workflow instance so the engine stops evaluating it
    try:
        instance_manager = WorkflowInstanceManager(context.db)
        instance_manager.set_enabled(session_id, workflow_name, enabled=False)
    except Exception as e:
        logger.warning("Could not disable workflow instance: %s", e)
        # Workflow is conceptually ended even if DB persistence fails
        return {"ended": True, "workflow": workflow_name}

    logger.info(
        "Workflow '%s' disabled for session %s via end_workflow action", workflow_name, session_id
    )
    return {"ended": True, "workflow": workflow_name}
