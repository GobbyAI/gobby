"""Expanded acceptance artifacts preserve behavior prose without requiring shell runs."""

from pathlib import Path

import pytest

from gobby.plans._artifact_refs import artifact_referenced
from gobby.plans.parser import parse_plan
from gobby.tasks.criterion_commands import authored_criterion_commands
from gobby.tasks.expansion._contract import _build_contract_entry_work_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("acceptance", "phrase"),
    [
        (f'Document login. behavior: "{phrase}"', phrase)
        for phrase in ["gobby auth login", "gobby auth key --mint", "git reset"]
    ]
    + [
        ('Running `gobby auth login` stores the key. behavior: "stores the key"', "stores the key"),
        ("Document login. behavior: `gobby auth login`", "gobby auth login"),
    ],
)
def test_behavior_artifact_does_not_become_close_command(
    tmp_path: Path, acceptance: str, phrase: str
) -> None:
    plan_path = tmp_path / "behavior.md"
    plan_path.write_text(
        f"""**Plan ID:** behavior

## A1 Behavior contract
`kind: deliverable`
Targets:
- `docs/auth.md`

**Acceptance:**
- A1.1 - {acceptance} in `docs/auth.md`.

## M1 Task Manifest
`kind: manifest`
```yaml
- title: Behavior contract
  category: docs
  task_type: chore
  depends_on: []
  validation_criteria: "Run `uv run pytest tests/auth/test_cli.py -q`."
  labels: [covers:behavior:A1:A1.1]
  tdd: false
  source_section: A1
```
""",
        encoding="utf-8",
    )
    plan = parse_plan(plan_path, parse_mode="expansion")
    leaf = _build_contract_entry_work_task(
        None,
        plan_doc=plan,
        entry=plan.manifest_entries[0],
        section=plan.sections[0],
        phase_id="phase-p1",
    )

    assert authored_criterion_commands(leaf["validation"]) == [
        "uv run pytest tests/auth/test_cli.py -q"
    ]
    assert f'behavior: "{phrase}" in `docs/auth.md`' in leaf["validation"]
    assert artifact_referenced(plan.sections[0].acceptance_items[0], leaf["validation"])
    assert leaf["affected_files"] == ["docs/auth.md"]
