"""Record-time refusal of review evidence by a task's implementer.

The split between implementation and review kinds lives in the selected closure rule's
`attestation_authorship`. This module reads that declaration and names no kind.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_shapes import CLOSURE_EVENTS, select_closure_rules
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody

if TYPE_CHECKING:
    from gobby.tasks.attestations import TaskAttestation


def attestation_authorship_refusal(
    *,
    kind: str,
    session_id: str,
    records: Sequence["TaskAttestation"],
    agent: AgentDefinitionBody,
    rules: list[RuleDefinitionRow],
) -> str | None:
    """Refuse a review kind from the task's implementer, or implementation from a reviewer.

    Every record counts, superseded records included, so a correction never erases
    authorship. Only rules the author's definition selects apply.
    """
    for row in select_closure_rules(agent, rules, CLOSURE_EVENTS):
        authorship = RuleDefinitionBody.model_validate(row.definition_json).attestation_authorship
        if authorship is None:
            continue
        implementation = authorship.implementation_kind
        if kind == implementation:
            reviewers = {r.session_id for r in records if r.kind not in authorship.maker_kinds}
            if session_id in reviewers:
                return (
                    f"{row.name} rule: this session already recorded review on this task, so it "
                    f"cannot record {kind!r}. Route the implementation to a seat that did not "
                    "review it."
                )
            continue
        if kind in authorship.maker_kinds:
            continue
        implementers = {r.session_id for r in records if r.kind == implementation}
        if session_id in implementers:
            return (
                f"{row.name} rule: this session recorded {implementation!r} on this task, so it "
                f"cannot record review {kind!r}. Route the review to an independent seat."
            )
    return None
