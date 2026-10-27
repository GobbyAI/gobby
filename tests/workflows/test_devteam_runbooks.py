"""The runbooks guide opens with the cold-start commands for the operator's personas."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS_GUIDE = ROOT / "docs/guides/runbooks.md"


def test_runbooks_guide_opens_with_the_cold_start() -> None:
    guide = RUNBOOKS_GUIDE.read_text()
    opening = guide.split("\n## ", 2)[1]
    for command in ("/gobby admin", "$gobby admin", "/gobby assistant", "$gobby assistant"):
        assert command in opening, command
    assert "Support" in guide and "DevTeam" in guide
