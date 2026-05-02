"""Phase 2 red contracts for the stage transition legality matrix."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import assert_stage_contract

pytestmark = pytest.mark.unit

ILLEGAL_TRANSITION_CASES = (
    ("ready", "complete_stage", "none", "skipping ready directly to done"),
    ("done", "start_stage", "none", "reverse transition from done"),
    ("in_progress", "submit_for_review", "none", "review submission on policy none"),
    ("needs_review", "approve_review", "none", "review approval on policy none"),
    ("needs_review", "reject_review", "none", "review rejection on policy none"),
    (
        "in_progress",
        "complete_stage",
        "required",
        "required-policy completion without validation_override_reason",
    ),
)


@pytest.mark.parametrize(
    ("state", "transition", "policy", "expectation"),
    ILLEGAL_TRANSITION_CASES,
)
def test_illegal_transition_matrix_rows(
    state: str,
    transition: str,
    policy: str,
    expectation: str,
) -> None:
    assert_stage_contract(
        f"{transition} from {state} under {policy} policy raises: {expectation}",
        required_symbols=(
            "gobby.storage.tasks._stage_states:IllegalStageTransitionError",
            "gobby.storage.tasks._stage_states:StageStatesManager",
        ),
    )
