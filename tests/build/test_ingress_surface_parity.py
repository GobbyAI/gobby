"""Phase 2 red contracts for stage-cap ingress parity."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_cli_mcp_http_resolve_to_same_StageManifestSpec_list_with_stage_caps": (
            "CLI, MCP, and HTTP build ingress resolve equivalent stage_caps inputs to "
            "identical StageManifestSpec lists"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:StageManifestSpec",),
)
