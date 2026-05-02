"""Pre-drop web audit for legacy task-state reads."""

from __future__ import annotations

import re

import pytest

from tests.phase5_contract_helpers import git_grep

pytestmark = pytest.mark.integration

WEB_LEGACY_PATTERN = (
    r"\blifecycle_stage\b|\bLifecycle\.|\bTaskBucket\b|"
    r"\bTASK_BUCKET_(LABELS|ORDER)\b|\bmoveTaskToBucket\b|"
    r"\bgetTaskBucket\b|\bKanbanBoard\b|\.lifecycle_stage\b|\bstate\.lifecycle\b"
)


def test_no_legacy_web_reads() -> None:
    result = git_grep(WEB_LEGACY_PATTERN, "web/src")
    allowed = (
        "test_legacy_symbols_removed.test.ts",
        "lifecycle-board-css-lint.test.ts",
    )
    unexpected = [
        line for line in result.stdout.splitlines() if not any(name in line for name in allowed)
    ]

    assert unexpected == []


def test_grep_does_not_match_new_lifecycle_board_identifiers() -> None:
    regex = re.compile(WEB_LEGACY_PATTERN)

    assert regex.search("LifecycleBoard lifecycle-board lifecycle-board:hide-blocked") is None
