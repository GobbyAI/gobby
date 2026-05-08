"""Phase 2 red contracts for build config stage caps."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_buildconfig_carries_optional_stage_caps_map": (
            "BuildConfig carries optional stage_caps overrides keyed by registry stage name"
        ),
        "test_buildconfig_excludes_default_max_review_rounds_accessor": (
            "BuildConfig removes the legacy default_max_review_rounds accessor"
        ),
        "test_buildconfig_excludes_max_review_rounds_field": (
            "BuildConfig removes legacy flat max_review_rounds"
        ),
        "test_yaml_normalizer_emits_deprecation_log_per_field": (
            "legacy flat cap YAML normalization logs one deprecation per migrated field"
        ),
        "test_yaml_normalizer_never_emits_qa_or_review_stage_names": (
            "legacy cap normalization maps only to registry stages, never qa or review"
        ),
        "test_yaml_normalizer_translates_max_expansion_attempts_to_stage_caps": (
            "max_expansion_attempts normalizes to expansion.max_work_attempts"
        ),
        "test_yaml_normalizer_translates_max_holistic_rounds_to_stage_caps": (
            "max_holistic_rounds normalizes to holistic_qa.max_review_rounds"
        ),
        "test_yaml_normalizer_translates_max_merge_attempts_to_stage_caps": (
            "max_merge_attempts normalizes to merge.max_work_attempts"
        ),
        "test_yaml_normalizer_translates_max_qa_rounds_to_stage_caps": (
            "max_qa_rounds normalizes to development.max_review_rounds"
        ),
        "test_yaml_normalizer_translates_max_review_rounds_to_stage_caps": (
            "max_review_rounds normalizes to pr.max_review_rounds"
        ),
    },
    required_symbols=("gobby.config.build:StageCapOverride",),
)
