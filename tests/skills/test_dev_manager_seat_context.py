"""The Dev Manager procedure and spawn_agent's accepted seat_context keys agree (#25369)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.spawn_agent._factory import SEAT_CONTEXT_KEYS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCEDURE = (
    REPO_ROOT / "src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md"
)
_SEAT_CONTEXT_LITERAL = re.compile(r"seat_context=(\{[^}]*\})")
_KEY = re.compile(r'"([a-z_]+)"\s*:')


def test_dev_manager_procedure_passes_only_accepted_seat_context_keys() -> None:
    documented = {
        key
        for literal in _SEAT_CONTEXT_LITERAL.findall(PROCEDURE.read_text(encoding="utf-8"))
        for key in _KEY.findall(literal)
    }

    assert "team_id" in documented
    assert documented <= SEAT_CONTEXT_KEYS
