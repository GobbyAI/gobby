"""Phase 2 red contracts for HTTP build stage caps."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_buildoptions_carries_typed_stagecapoverride_list": (
            "HTTP BuildOptions exposes typed stage_caps overrides"
        ),
        "test_buildoptions_excludes_legacy_flat_fields": (
            "HTTP BuildOptions removes legacy flat cap fields"
        ),
        "test_route_forwards_to_shared_service": (
            "HTTP build route forwards stage_caps to the shared build service"
        ),
    },
    required_symbols=("gobby.servers.routes.build:StageCapOverride",),
)
