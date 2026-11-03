"""Bundled turn-end gates must not demand work from a seat inside an announced hold.

A restart or cutover hold is announced as a durable wait (``wait_for_coordination``
on the announcer). A gate that blocks turn end on every attempt, with no bounded
acknowledgement, would keep a held seat looping while the work it demands is
paused and ``no-bash-sleep`` blocks any wait loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_HOLD_EXEMPTIONS = ("has_durable_stop_wait()", "has_active_coordination_wait()")


def _unbounded_turn_end_blocks() -> list[tuple[Path, str, dict[str, Any]]]:
    gates = []
    for path in sorted(get_bundled_rules_path().rglob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            continue
        for name, rule in (document.get("rules") or {}).items():
            if not isinstance(rule, dict) or rule.get("event") not in {"turn_end", "stop"}:
                continue
            for effect in rule.get("effects") or []:
                # An acknowledged or on-receipt block fires once and then lets the turn end.
                if (
                    isinstance(effect, dict)
                    and effect.get("type") == "block"
                    and not effect.get("acknowledge_variable")
                    and effect.get("delivery") != "on_receipt"
                ):
                    gates.append((path, name, rule))
                    break
    return gates


def test_bundled_rules_include_unbounded_turn_end_gates() -> None:
    assert _unbounded_turn_end_blocks()


@pytest.mark.parametrize(
    ("path", "name", "rule"),
    [
        pytest.param(*gate, id=f"{gate[0].parent.name}/{gate[1]}")
        for gate in _unbounded_turn_end_blocks()
    ],
)
def test_unbounded_turn_end_gate_yields_to_a_durable_hold(
    path: Path, name: str, rule: dict[str, Any]
) -> None:
    condition = str(rule.get("when") or "")
    assert any(exemption in condition for exemption in _HOLD_EXEMPTIONS), (
        f"{path.name}:{name} blocks turn end without yielding to a durable hold"
    )
