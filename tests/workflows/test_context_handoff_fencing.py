"""Injected-context fencing is scoped to the session_start handoff templates.

The contamination fix fences handoff/compact summaries with sentinels so the
digest/summary pipeline strips them. Fencing must live in those two templates
only — not in every ``inject_context`` effect — so per-turn injections (brevity,
memory, task context) stay un-tagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import gobby
from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN, INJECTED_CONTEXT_END

pytestmark = pytest.mark.unit

_RULES_DIR = Path(gobby.__file__).parent / "install" / "shared" / "workflows" / "rules"
_HANDOFF_DIR = _RULES_DIR / "context-handoff"


def _inject_template(path: Path, rule_name: str) -> str:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    effects = data["rules"][rule_name]["effects"]
    templates = [e["template"] for e in effects if e.get("type") == "inject_context"]
    assert templates, f"{rule_name} has no inject_context effect"
    return "\n".join(templates)


@pytest.mark.parametrize(
    ("filename", "rule_name"),
    [
        ("inject-previous-session-summary.yaml", "inject-previous-session-summary"),
        ("inject-compact-handoff.yaml", "inject-compact-handoff"),
    ],
)
def test_handoff_templates_are_fenced(filename: str, rule_name: str) -> None:
    template = _inject_template(_HANDOFF_DIR / filename, rule_name)

    assert INJECTED_CONTEXT_BEGIN in template
    assert INJECTED_CONTEXT_END in template
    assert template.index(INJECTED_CONTEXT_BEGIN) < template.index(INJECTED_CONTEXT_END)


def test_per_turn_brevity_template_is_not_fenced() -> None:
    template = _inject_template(
        _RULES_DIR / "brevity" / "reinforce-brevity.yaml", "remind-brevity-on-turn-start"
    )

    assert INJECTED_CONTEXT_BEGIN not in template
    assert INJECTED_CONTEXT_END not in template
