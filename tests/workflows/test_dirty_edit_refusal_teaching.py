"""Rule 3's unowned-dirt refusal teaches its likely cause and recovery (#24956).

A seat that rewrites a repository file through an interpreter script leaves the
change unattributable (#23884 B1), so its own next Edit is refused as unowned
dirt. The refusal must say so, and say how to recover, instead of only naming
an operator command.
"""

from __future__ import annotations

import re

import pytest

from gobby.workflows.commit_guard_conflicts import format_dirty_edit_reason
from gobby.workflows.dirty_edit_grants import dirty_path_approval_command

pytestmark = pytest.mark.unit

_PATH = ".gobby/plans/gdaemon-cron-family.md"


def _unowned_reason() -> str:
    command = dirty_path_approval_command("session-1", "/repo", [_PATH])
    return format_dirty_edit_reason(set(), {_PATH}, [command])


def test_unowned_refusal_names_the_opaque_interpreter_write_cause() -> None:
    reason = _unowned_reason()

    assert f"- {_PATH} — no live session owns it" in reason
    assert re.search(r"interpreter", reason, re.IGNORECASE), reason
    assert re.search(r"script", reason, re.IGNORECASE), reason
    # The writer may be the refused seat itself; today's header says only "outside this session".
    assert re.search(r"your own|including (this|your)", reason, re.IGNORECASE), reason


def test_unowned_refusal_routes_recovery_through_the_operator_then_edit_write() -> None:
    reason = _unowned_reason()

    assert "gobby sessions approve-dirty-paths session-1" in reason
    assert "Triage" in reason, reason
    assert "Assistant" in reason, reason
    assert re.search(r"\brevert", reason, re.IGNORECASE), reason
    assert re.search(r"\bEdit\b", reason) and re.search(r"\bWrite\b", reason), reason
