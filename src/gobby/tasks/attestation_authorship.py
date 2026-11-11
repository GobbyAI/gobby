"""Record-time refusal of review evidence by a task's implementer.

The split between implementation and review kinds lives in the selected closure rule's
`attestation_authorship`. This module reads that declaration and names no kind.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_shapes import CLOSURE_EVENTS, select_closure_rules
from gobby.workflows.agent_models import WAIVE_REVIEW_GATES, AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody

if TYPE_CHECKING:
    from gobby.tasks.attestations import EvidenceReference, TaskAttestation


def _candidate(reference: "EvidenceReference | None") -> str | None:
    """A candidate is its value; the same commit under another reference scheme is the same one."""
    return None if reference is None else reference.value


def attestation_authorship_refusal(
    *,
    kind: str,
    session_id: str,
    records: Sequence["TaskAttestation"],
    reference: "EvidenceReference | None",
    agent: AgentDefinitionBody,
    rules: list[RuleDefinitionRow],
) -> str | None:
    """Refuse a review kind from the task's implementer, or implementation from a reviewer.

    Every record counts, superseded records included, so a correction never erases
    authorship. A reviewer is refused implementation of the candidate it reviewed, matched by
    value whatever the reference scheme. A review that names a different candidate than the
    implementation does not count; a review that names no candidate counts against every
    implementation. Only rules the author's definition selects apply.
    """
    if agent.holds(WAIVE_REVIEW_GATES):
        return None
    candidate = _candidate(reference)
    for row in select_closure_rules(agent, rules, CLOSURE_EVENTS):
        authorship = RuleDefinitionBody.model_validate(row.definition_json).attestation_authorship
        if authorship is None:
            continue
        implementation = authorship.implementation_kind
        if kind == implementation:
            reviewers = {
                r.session_id
                for r in records
                if r.kind not in authorship.maker_kinds
                and (candidate is None or _candidate(r.reference) in (None, candidate))
            }
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
