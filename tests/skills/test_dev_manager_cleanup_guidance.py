"""The Dev Manager cleans up nothing; the daemon deletes a team's trees on close (#25224)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROCEDURE = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md"
)
_WHITESPACE_RUN = re.compile(r"\s+")
_CLEANS_NOTHING_UP = re.compile(r"clean(s)? nothing up", re.IGNORECASE)


def _procedure_text() -> str:
    return _WHITESPACE_RUN.sub(" ", PROCEDURE.read_text(encoding="utf-8"))


def test_dev_manager_never_deletes_a_sub_worktree() -> None:
    assert "delete_worktree" not in _procedure_text()


def test_dev_manager_cleans_nothing_up_in_either_mode() -> None:
    body = _procedure_text()
    single_mode = body.partition("## Reviewer routing")[0]
    epic_mode = body.partition("## Epic mode")[2]

    assert _CLEANS_NOTHING_UP.search(single_mode)
    assert _CLEANS_NOTHING_UP.search(epic_mode)
