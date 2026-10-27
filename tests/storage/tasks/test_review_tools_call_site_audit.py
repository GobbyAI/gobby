"""Post-cutover review-tool call-site audit contracts."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_no_residual_success_hook_keyed_on_unused_tool() -> None:
    source = source_texts(("src/gobby/install/shared",))

    assert "on_mcp_success: approve_review" not in source


def test_no_legacy_status_prose_remains_in_skill_or_rule_yaml() -> None:
    source = source_texts(("src/gobby/install/shared/skills", "src/gobby/install/shared/workflows"))

    assert "status becomes needs_review" not in source
    assert "stage stays open" not in source


def test_post_cutover_allowlist_matches_phase_2_6_6() -> None:
    source = source_texts(("src/gobby/install/shared",))

    assert "test-architect.yaml" not in source or "submit_for_review" not in source
