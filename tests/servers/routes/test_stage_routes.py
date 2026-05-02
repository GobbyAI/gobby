"""Phase 2 red contracts for stage HTTP routes."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_list_filter_5_state_values": (
            "task list routes accept every stage state filter value"
        ),
        "test_list_filter_by_stage_state": (
            "task list routes filter by exact stage_name and stage state"
        ),
        "test_list_includes_denormalized_manifest": (
            "task list responses include denormalized manifest stage rows"
        ),
        "test_patch_422_payload_uses_illegal_manifest_mutation_discriminator": (
            "PATCH stage mutation errors include the IllegalManifestMutationError discriminator"
        ),
        "test_patch_add_at_current_position_returns_422": (
            "PATCH add-stage at current position returns 422 with mutation payload"
        ),
        "test_patch_add_existing_stage_returns_422": (
            "PATCH add-stage existing row returns 422 with mutation payload"
        ),
        "test_patch_illegal_transition_returns_422_with_payload": (
            "PATCH illegal transition returns 422 with transition payload"
        ),
        "test_patch_mutation_on_exhausted_manifest_returns_422": (
            "PATCH mutation on exhausted manifest returns 422"
        ),
        "test_patch_remove_done_returns_422": "PATCH remove done row returns 422",
        "test_patch_remove_in_progress_returns_422": ("PATCH remove in_progress row returns 422"),
        "test_patch_remove_last_future_row_returns_would_exhaust_422": (
            "PATCH remove last future row returns would_exhaust_terminal_position"
        ),
        "test_patch_remove_missing_stage_returns_422": ("PATCH remove missing stage returns 422"),
        "test_patch_start_stage": "PATCH start-stage delegates to StageStatesManager.start_stage",
        "test_routes_registered": "stage HTTP routes are registered on the app",
    },
    required_symbols=("gobby.servers.routes.stage_routes:router",),
)
