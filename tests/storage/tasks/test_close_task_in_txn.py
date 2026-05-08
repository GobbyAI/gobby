"""Phase 2 red contracts for the transaction-aware close helper."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_bootstrap_ledger_validation_runs_inside_helper": (
            "_close_task_in_txn performs bootstrap-ledger validation inside the helper"
        ),
        "test_close_task_public_api_passes_cascade_false": (
            "public close_task delegates with cascade_descendants=False"
        ),
        "test_close_task_public_wrapper_maps_closed_commit_sha_to_commit_sha": (
            "public closed_commit_sha boundary maps to helper commit_sha"
        ),
        "test_complete_stage_merge_terminal_passes_cascade_true": (
            "complete_stage passes cascade_descendants=True only for merge terminal rows"
        ),
        "test_complete_stage_non_merge_terminal_passes_cascade_false": (
            "complete_stage passes cascade_descendants=False for non-merge terminals"
        ),
        "test_force_and_validation_override_pass_through": (
            "force and validation_override_reason pass through to _close_task_in_txn"
        ),
        "test_helper_signature_accepts_all_canonical_params": (
            "_close_task_in_txn accepts reason, commit_sha, closed_at, closed_in_session_id, "
            "force, cascade_descendants, and validation_override_reason"
        ),
        "test_helper_uses_commit_sha_keyword_not_closed_commit_sha": (
            "_close_task_in_txn helper-side spelling is commit_sha, not closed_commit_sha"
        ),
        "test_open_child_check_runs_inside_helper": (
            "_close_task_in_txn runs open-child checks inside the helper transaction"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:_close_task_in_txn",),
)
