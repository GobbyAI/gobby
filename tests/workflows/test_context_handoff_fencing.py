"""Injected-context fencing is scoped to the handoff templates.

The contamination fix fences handoff/compact/clear summaries with sentinels so
the digest/summary pipeline strips them. Fencing must live in those templates
only — not in every ``inject_context`` effect — so per-turn injections (brevity,
memory, task context) stay un-tagged.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

import gobby
from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN, INJECTED_CONTEXT_END
from gobby.workflows.engine.effects import EffectsMixin

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
        ("inject-clear-handoff.yaml", "inject-clear-handoff-on-prompt"),
        ("inject-compact-handoff.yaml", "inject-compact-handoff"),
    ],
)
def test_handoff_templates_are_fenced(filename: str, rule_name: str) -> None:
    template = _inject_template(_HANDOFF_DIR / filename, rule_name)

    assert INJECTED_CONTEXT_BEGIN in template
    assert INJECTED_CONTEXT_END in template
    assert template.index(INJECTED_CONTEXT_BEGIN) < template.index(INJECTED_CONTEXT_END)


def test_engine_inject_context_comment_cites_live_handoff_templates() -> None:
    source = inspect.getsource(EffectsMixin._apply_effect)
    assert "inject-previous-session-summary" not in source
    assert "inject-clear-handoff.yaml" in source


@pytest.mark.parametrize(
    ("group", "filename", "rule_name", "level_var"),
    [
        ("brevity", "reinforce-brevity.yaml", "remind-brevity-on-turn-start", "brevity_level"),
        (
            "restraint",
            "require-restraint-skill.yaml",
            "remind-restraint-on-turn-start",
            "restraint_level",
        ),
    ],
)
def test_per_turn_reminder_template_is_not_fenced_and_interpolates_level(
    group: str, filename: str, rule_name: str, level_var: str
) -> None:
    template = _inject_template(_RULES_DIR / group / filename, rule_name)

    assert INJECTED_CONTEXT_BEGIN not in template
    assert INJECTED_CONTEXT_END not in template
    assert f"{{{{ {level_var} }}}}" in template
