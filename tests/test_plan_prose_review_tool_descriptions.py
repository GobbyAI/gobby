"""Bundled guidance must not describe review tools as complete/fail shims."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_no_complete_stage_fail_stage_shim_prose_in_runtime_guidance() -> None:
    guidance = source_texts(("src/gobby/install/shared", "src/gobby/mcp_proxy/tools/tasks"))

    assert "complete_stage / fail_stage shims" not in guidance
    assert "complete_stage/fail_stage shims" not in guidance
    assert "composing `complete_stage` / `fail_stage`" not in guidance
