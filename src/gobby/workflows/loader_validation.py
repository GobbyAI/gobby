"""Pipeline reference validation helpers for WorkflowLoader.

Extracted from loader.py as part of Strangler Fig decomposition (Wave 2).
"""

import re
from typing import Any

_COMPLETED_STATUS_CONDITION_RE = re.compile(
    r"\bsteps\.([a-zA-Z_][a-zA-Z0-9_]*)\.output\.([a-zA-Z_][a-zA-Z0-9_.]*)\s*"
    r"(==|!=)\s*['\"]completed['\"]"
)


def _validate_pipeline_references(data: dict[str, Any]) -> None:
    """
    Validate that all $step_id.output references in a pipeline refer to earlier steps.

    Args:
        data: Pipeline data dictionary

    Raises:
        ValueError: If a reference points to a non-existent or later step
    """
    steps = data.get("steps", [])
    step_ids = [s.get("id") for s in steps if s.get("id")]

    # Build set of valid step IDs that can be referenced at each position
    valid_at_position: dict[int, set[str]] = {}
    for i in range(len(step_ids)):
        # Steps at position i can only reference steps 0..i-1
        valid_at_position[i] = set(step_ids[:i])

    # Validate references in each step
    for i, step in enumerate(steps):
        step_id = step.get("id", f"step_{i}")
        valid_refs = valid_at_position.get(i, set())

        # Check prompt field
        if "prompt" in step and step["prompt"]:
            refs = _extract_step_refs(step["prompt"])
            _check_refs(refs, valid_refs, step_ids, step_id, "prompt")

        # Check condition field
        if "condition" in step and step["condition"]:
            refs = _extract_step_refs(step["condition"])
            _check_refs(refs, valid_refs, step_ids, step_id, "condition")

        # Check input field
        if "input" in step and step["input"]:
            refs = _extract_step_refs(step["input"])
            _check_refs(refs, valid_refs, step_ids, step_id, "input")

        # Check exec field (might have embedded references)
        if "exec" in step and step["exec"]:
            refs = _extract_step_refs(step["exec"])
            _check_refs(refs, valid_refs, step_ids, step_id, "exec")

    _validate_fail_pipeline_completion_order(steps)

    # Validate references in pipeline outputs (can reference any step)
    all_step_ids = set(step_ids)
    outputs = data.get("outputs", {})
    for output_name, output_value in outputs.items():
        if isinstance(output_value, str):
            refs = _extract_step_refs(output_value)
            for ref in refs:
                if ref not in all_step_ids:
                    raise ValueError(
                        f"Pipeline output '{output_name}' references unknown step '{ref}'. "
                        f"Valid steps: {sorted(all_step_ids)}"
                    )


def _extract_step_refs(text: str) -> set[str]:
    """
    Extract step IDs from $step_id.output patterns in text.

    Args:
        text: Text to search for references

    Returns:
        Set of step IDs referenced
    """
    # Match $step_id.output or $step_id.output.field patterns
    # Exclude $inputs.* which are input references, not step references
    pattern = r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.(output|approved)"
    matches = re.findall(pattern, text)
    # Filter out 'inputs' which is a special reference
    return {m[0] for m in matches if m[0] != "inputs"}


def _check_refs(
    refs: set[str],
    valid_refs: set[str],
    all_step_ids: list[str],
    current_step: str,
    field_name: str,
) -> None:
    """
    Check that all references are valid.

    Args:
        refs: Set of referenced step IDs
        valid_refs: Set of step IDs that can be referenced (earlier steps)
        all_step_ids: List of all step IDs in the pipeline
        current_step: Current step ID (for error messages)
        field_name: Field name being checked (for error messages)

    Raises:
        ValueError: If any reference is invalid
    """
    for ref in refs:
        if ref not in valid_refs:
            if ref in all_step_ids:
                # It's a forward reference
                raise ValueError(
                    f"Step '{current_step}' {field_name} references step '{ref}' "
                    f"which appears later in the pipeline. Steps can only reference "
                    f"earlier steps. Valid references: {sorted(valid_refs) if valid_refs else '(none)'}"
                )
            else:
                # It's a non-existent step
                raise ValueError(
                    f"Step '{current_step}' {field_name} references unknown step '{ref}'. "
                    f"Valid steps: {sorted(all_step_ids)}"
                )


def _completed_status_conditions(condition: Any) -> set[tuple[str, str, str]]:
    """Return completed-status comparisons from a pipeline step condition.

    The tuple contains ``(step_id, output_field, operator)`` for expressions like
    ``steps.review.output.status == 'completed'``. Non-string conditions have no
    comparable completed-status checks.
    """
    if not isinstance(condition, str):
        return set()
    return {
        (step_id, output_field, operator)
        for step_id, output_field, operator in _COMPLETED_STATUS_CONDITION_RE.findall(condition)
    }


def _is_fail_pipeline_step(step: dict[str, Any]) -> bool:
    """Return True when a step invokes the internal ``fail_pipeline`` MCP tool."""
    mcp = step.get("mcp")
    return isinstance(mcp, dict) and mcp.get("tool") == "fail_pipeline"


def _validate_fail_pipeline_completion_order(steps: list[dict[str, Any]]) -> None:
    """Reject fail branches placed after a matching completed success branch.

    A fail step that checks ``!= 'completed'`` for the same step output after a
    prior ``== 'completed'`` check is unreachable in the intended branch order.
    """
    completed_checks: set[tuple[str, str]] = set()
    for step in steps:
        checks = _completed_status_conditions(step.get("condition"))
        if _is_fail_pipeline_step(step):
            for step_id, output_field, operator in checks:
                if operator == "!=" and (step_id, output_field) in completed_checks:
                    current_step = step.get("id", "<unknown>")
                    raise ValueError(
                        f"fail_pipeline step '{current_step}' checks "
                        f"steps.{step_id}.output.{output_field} != 'completed' after a "
                        "matching completed check. Place fail_pipeline before the completed path."
                    )

        completed_checks.update(
            (step_id, output_field)
            for step_id, output_field, operator in checks
            if operator == "=="
        )
