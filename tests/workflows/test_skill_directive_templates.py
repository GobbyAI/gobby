"""Contract tests for bundled rule skill-fetch directives."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RULES_PATH = (
    Path(__file__).parents[2] / "src" / "gobby" / "install" / "shared" / "workflows" / "rules"
)


def test_rule_templates_do_not_duplicate_legacy_skill_fetch_directive() -> None:
    legacy_directive = 'Load the skill: call_tool("gobby-skills", "get_skill"'
    offenders = [
        path.relative_to(RULES_PATH).as_posix()
        for path in sorted(RULES_PATH.rglob("*.yaml"))
        if legacy_directive in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
