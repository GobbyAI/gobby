"""Phase 2 red contracts for manifest mutation legality."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_add_stage_after_current_succeeds": (
            "add_stage succeeds after the current position and shifts future rows"
        ),
        "test_add_stage_at_current_position_rejected": (
            "add_stage rejects insertion at the current position"
        ),
        "test_add_stage_before_current_rejected": (
            "add_stage rejects insertion before the current position"
        ),
        "test_add_stage_existing_row_rejected": (
            "add_stage rejects stage names already present in the manifest"
        ),
        "test_add_stage_on_exhausted_manifest_rejected": ("add_stage rejects exhausted manifests"),
        "test_add_stage_preserves_zero_indexed_dense_positions": (
            "successful add_stage leaves positions exactly range(N)"
        ),
        "test_current_stage_unchanged_after_allowed_add": (
            "allowed add_stage does not change the current stage pointer"
        ),
        "test_current_stage_unchanged_after_allowed_remove": (
            "allowed remove_stage does not change the current stage pointer"
        ),
        "test_illegal_mutation_error_carries_full_payload": (
            "IllegalManifestMutationError carries the documented seven-tuple payload"
        ),
        "test_mutation_emits_lifecycle_event_with_shape_signatures": (
            "manifest mutations emit lifecycle events with manifest shape signatures"
        ),
        "test_remove_done_row_rejected": "remove_stage rejects done rows",
        "test_remove_in_progress_rejected": "remove_stage rejects in_progress rows",
        "test_remove_last_future_row_rejected_would_exhaust": (
            "remove_stage rejects removing the last future terminal path"
        ),
        "test_remove_missing_stage_rejected": (
            "remove_stage rejects stage names not present in the manifest"
        ),
        "test_remove_needs_review_rejected": "remove_stage rejects needs_review rows",
        "test_remove_on_exhausted_manifest_rejected": ("remove_stage rejects exhausted manifests"),
        "test_remove_review_approved_rejected": ("remove_stage rejects review_approved rows"),
        "test_remove_stage_at_current_rejected": ("remove_stage rejects the current position"),
        "test_remove_stage_before_current_rejected": (
            "remove_stage rejects rows before the current position"
        ),
        "test_remove_stage_future_ready_succeeds": (
            "remove_stage succeeds for future ready rows and reorders densely"
        ),
        "test_remove_stage_preserves_zero_indexed_dense_positions": (
            "successful remove_stage leaves positions exactly range(N-1)"
        ),
    },
    required_symbols=(
        "gobby.storage.tasks._stage_states:IllegalManifestMutationError",
        "gobby.storage.tasks._stage_states:StageStatesManager",
    ),
)
