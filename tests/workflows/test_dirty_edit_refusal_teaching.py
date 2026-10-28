"""Rule 3's unowned-dirt refusal names its likely cause and routes recovery to Triage (#24956).

A seat that rewrites a repository file through an interpreter script in a main or
shared checkout leaves the change unattributed, so its own next Edit is refused as
unowned dirt. The refusal must name that cause and route recovery to Triage. It must
not name the operator-only approval command.
"""

from __future__ import annotations

import re

import pytest

from gobby.workflows.commit_guard_conflicts import format_dirty_edit_reason

pytestmark = pytest.mark.unit

_PATH = ".gobby/plans/gdaemon-cron-family.md"


def _unowned_reason() -> str:
    return format_dirty_edit_reason(set(), {_PATH})


def test_unowned_refusal_names_the_opaque_interpreter_write_cause() -> None:
    reason = _unowned_reason()

    assert f"- {_PATH} — no live session owns it" in reason
    assert re.search(r"interpreter", reason, re.IGNORECASE), reason
    assert re.search(r"script", reason, re.IGNORECASE), reason
    assert "outside a task worktree" in reason, reason


def test_unowned_refusal_routes_recovery_to_triage_without_the_approval_command() -> None:
    reason = _unowned_reason()

    assert "approve-dirty-paths" not in reason
    assert "Triage" in reason, reason
    assert re.search(r"\brevert", reason, re.IGNORECASE), reason
    assert "Then retry." not in reason.splitlines()
