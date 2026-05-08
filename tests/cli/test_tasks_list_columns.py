"""CLI task list output drops legacy status/lifecycle columns."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_no_legacy_columns() -> None:
    package_dir = Path("src/gobby/cli/tasks/_utils")
    source = "\n".join(source_text(str(p)) for p in sorted(package_dir.glob("*.py")))

    assert "lifecycle_stage" not in source
    assert "COL_STATUS" not in source
