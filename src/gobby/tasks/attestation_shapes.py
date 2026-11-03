"""Selected closure rules and the attestation shapes they declare for record time."""

import re
from collections.abc import Collection
from typing import Any

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.agent_models import AgentDefinitionBody, AttestationKind
from gobby.workflows.definitions import (
    ATTESTATION_SHAPE_FUNCTIONS,
    AttestationShape,
    RuleDefinitionBody,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.selectors import resolve_rules_for_agent

CLOSURE_EVENTS = ("task_close", "task_land")


def select_closure_rules(
    agent: AgentDefinitionBody, rules: list[RuleDefinitionRow], events: Collection[str]
) -> list[RuleDefinitionRow]:
    """The enabled rules the definition selects for these events, in evaluation order."""
    available = [row for row in rules if row.enabled and row.deleted_at is None]
    selected_names = resolve_rules_for_agent(agent, available)
    return sorted(
        (
            row
            for row in available
            if row.name in selected_names and row.definition_json.get("event") in events
        ),
        key=lambda row: (row.priority, row.name, row.id),
    )


def attestation_shape_refusal(
    *,
    kind: str,
    result: str,
    scheme: str | None,
    value: str | None,
    task: dict[str, Any],
    agent: AgentDefinitionBody,
    declared: AttestationKind | None,
    rules: list[RuleDefinitionRow],
) -> str | None:
    """Explain why no selected closure rule could ever accept this evidence, if so.

    A result the author's definition declares for the kind stays recordable even when
    no rule accepts it, so truthful negative evidence is never refused. Record time
    fails open on a shape it cannot evaluate; the closure gate stays authoritative.
    """
    evaluator = SafeExpressionEvaluator({"task": task}, dict(ATTESTATION_SHAPE_FUNCTIONS))
    shapes: list[tuple[str, str, AttestationShape]] = []
    for row in select_closure_rules(agent, rules, CLOSURE_EVENTS):
        try:
            shape = RuleDefinitionBody.model_validate(row.definition_json).attestation_shape
            if shape is None or kind not in shape.kinds:
                continue
            if shape.applies_when is not None and not evaluator.evaluate(shape.applies_when):
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        shapes.append((row.name, str(row.definition_json["event"]), shape))
    problems: list[str] = []
    for name, event, shape in shapes:
        if shape.result is not None and result != shape.result:
            if declared is not None and result in declared.allowed_results:
                return None
            problems.append(
                f"{event} rule {name} needs result {shape.result!r}, got result {result!r}"
            )
        reference = shape.reference
        if reference is None:
            continue
        if scheme is None or value is None:
            problems.append(f"{event} rule {name} needs a reference, got no reference")
        elif scheme != reference.scheme:
            problems.append(
                f"{event} rule {name} needs reference scheme {reference.scheme!r}, "
                f"got reference scheme {scheme!r}"
            )
        elif re.fullmatch(reference.value_pattern, value) is None:
            problems.append(
                f"{event} rule {name} needs a reference value matching "
                f"{reference.value_pattern!r}, got reference value {value!r}"
            )
    if not problems:
        return None
    accepted = []
    for _name, _event, shape in shapes:
        if shape.result is not None:
            accepted.append(f"result {shape.result!r}")
        if shape.reference is not None:
            accepted.append(
                f"reference {{scheme {shape.reference.scheme!r}, value matching "
                f"{shape.reference.value_pattern!r}}}"
            )
    return (
        f"Selected closure rules can never accept this {kind} attestation: "
        f"{'; '.join(problems)}. Accepted shape: {', '.join(dict.fromkeys(accepted))}."
    )
