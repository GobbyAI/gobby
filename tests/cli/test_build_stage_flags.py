"""Phase 2 red contracts for build stage flags."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_build_constructs_StageManifestSpec_with_caps": (
            "gobby build constructs StageManifestSpec rows carrying per-stage cap overrides"
        ),
        "test_build_flag_resolution": (
            "gobby build --stage, --skip-stage, and per-stage cap settings "
            "resolve to a deterministic manifest"
        ),
        "test_initialize_manifest_persists_caps_atomically": (
            "build initialization persists per-stage caps inside the same transaction as "
            "manifest row creation"
        ),
        "test_per_stage_cap_overrides_persisted_on_state_row": (
            "per-stage cap overrides are mirrored onto task_stage_states rows at init time"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:StageManifestSpec",),
)
