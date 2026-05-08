"""Phase 2 red contracts for add_stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_error_payload_carries_full_seven_tuple": (
            "add_stage surfaces IllegalManifestMutationError's seven-tuple payload"
        ),
        "test_insert_at_or_before_current_returns_typed_error": (
            "add_stage at or before the current stage returns a typed mutation error"
        ),
        "test_insert_existing_stage_returns_typed_error": (
            "add_stage for an existing manifest stage returns a typed mutation error"
        ),
        "test_insert_mid_manifest_reorders": (
            "add_stage inserts future rows and preserves dense manifest positions"
        ),
        "test_insert_on_exhausted_manifest_returns_typed_error": (
            "add_stage on an exhausted manifest returns a typed mutation error"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
