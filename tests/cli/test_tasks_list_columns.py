"""CLI task list output drops legacy status/lifecycle columns."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_no_legacy_columns() -> None:
    source = source_text("src/gobby/cli/tasks/_utils.py")

    assert "lifecycle_stage" not in source
    assert "COL_STATUS" not in source
