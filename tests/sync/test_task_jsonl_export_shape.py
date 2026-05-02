"""Task JSONL export drops legacy task state keys."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_no_legacy_keys() -> None:
    source = source_text("src/gobby/sync/tasks.py")

    for key in ('"status":', '"lifecycle":', '"lifecycle_stage":'):
        assert key not in source
