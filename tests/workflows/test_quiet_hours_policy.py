"""Quiet-hours policy and portable bundled activation guidance."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / ".gobby/workflows/rules/project-operating-policy.yaml"
ORCHESTRATOR = (
    ROOT / "src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md"
)


def test_project_policy_daily_and_monday_quiet_hours() -> None:
    document: dict[str, Any] = yaml.safe_load(POLICY.read_text())
    effects = document["rules"]["project-operating-policy"]["effects"]
    policy = next(
        effect["value"] for effect in effects if effect.get("variable") == "project_policy"
    )
    text = policy["quiet_hours"]
    assert "America/Chicago" in text
    normalized = text.lower().replace("–", "-").replace("—", "-")
    assert "04:00-05:00" in normalized, f"Missing daily quiet-hours window: {text}"
    assert "daily" in normalized
    assert "04:00-06:00" in normalized, f"Missing Monday quiet-hours extension: {text}"
    assert "monday" in normalized


def test_game_goblins_guidance_references_single_quiet_hours_value() -> None:
    document: dict[str, Any] = yaml.safe_load(POLICY.read_text())
    effects = document["rules"]["project-operating-policy"]["effects"]
    template = next(effect["template"] for effect in effects if effect["type"] == "inject_context")
    guidance = next(line for line in template.splitlines() if "Game Goblins" in line)
    assert "project_policy.quiet_hours" in guidance
    assert re.search(r"\b\d{1,2}:\d{2}\b", guidance) is None
    assert "no restart/cutover" in guidance
    assert "low load" in guidance
    assert "never rerun" in guidance

    # The structured value is the only policy location allowed to state the window.
    policy = next(
        effect["value"] for effect in effects if effect.get("variable") == "project_policy"
    )
    policy["quiet_hours"] = ""
    assert re.search(r"(?:04:00|05:00|06:00)", yaml.safe_dump(document)) is None


def test_bundled_landing_ledger_uses_project_policy_quiet_hours() -> None:
    ledger = next(
        line for line in ORCHESTRATOR.read_text().splitlines() if "- Landing ledger." in line
    )
    assert re.search(r"project(?:'s)?[ _.]policy", ledger, re.IGNORECASE), ledger
    assert "quiet" in ledger.lower() and "hours" in ledger.lower()
    assert "goblin" not in ledger.lower()
    assert re.search(r"\b\d{1,2}:\d{2}\b", ledger) is None


def test_live_configuration_has_no_retired_quiet_hours() -> None:
    roots = [ROOT / ".gobby/workflows", ROOT / "src/gobby/install/shared"]
    offenders = [
        str(path.relative_to(ROOT))
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and "06:45" in path.read_text(errors="replace")
    ]
    assert offenders == [], f"Retired quiet-hours end time remains in: {offenders}"
