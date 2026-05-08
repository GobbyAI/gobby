"""Phase 2 red contracts for remove_stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_error_payload_carries_full_seven_tuple": (
            "remove_stage surfaces IllegalManifestMutationError's seven-tuple payload"
        ),
        "test_remove_at_or_before_current_returns_typed_error": (
            "remove_stage rejects rows at or before the current position"
        ),
        "test_remove_done_returns_typed_error": "remove_stage rejects done rows",
        "test_remove_in_progress_returns_typed_error": "remove_stage rejects in_progress rows",
        "test_remove_last_future_row_returns_would_exhaust_error": (
            "remove_stage rejects removal that would exhaust the manifest's terminal path"
        ),
        "test_remove_missing_stage_returns_typed_error": (
            "remove_stage returns typed payloads for missing stage names"
        ),
        "test_remove_reorders_dense": ("remove_stage reorders remaining future positions densely"),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
