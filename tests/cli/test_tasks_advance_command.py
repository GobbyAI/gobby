"""Phase 2 red contracts for gobby tasks advance."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_advance_required_from_in_progress_errors": (
            "advance errors on required-policy in_progress rows and suggests review --submit"
        ),
        "test_advance_required_policy_from_review_approved": (
            "advance completes a required-policy review_approved row and starts the next row"
        ),
        "test_auto_advance_next_stage_policy_none": (
            "advance completes policy-none current rows and auto-starts the next eligible row"
        ),
    },
    required_paths=("src/gobby/cli/tasks/stages.py",),
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
