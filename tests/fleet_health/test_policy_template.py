"""The shipped project operating policy carries the fleet_health defaults and no load hold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_RULES_DIR = Path(__file__).resolve().parents[2] / ".gobby" / "workflows" / "rules"

# Mirrors Thresholds::default() in crates/gdaemon/src/fleet_health/policy.rs.
_DEFAULTS: dict[str, Any] = {
    "probe_degraded_ms": 1000,
    "probe_unstable_ms": 3000,
    "severe_unstable_window_seconds": 120,
    "window_seconds": 300,
    "warning_lines_degraded": 10,
    "load_per_core_degraded": 2.5,
    "load_consecutive_samples": 2,
    "memory_free_degraded_percent": 15.0,
    "fseventsd_cpu_degraded_percent": 50.0,
    "fseventsd_consecutive_samples": 3,
}


def _project_policy_writes() -> list[dict[str, Any]]:
    """Every value a rule file writes to project_policy, found without naming a rule."""
    values: list[dict[str, Any]] = []
    for path in sorted(_RULES_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rule in (document.get("rules") or {}).values():
            for effect in rule.get("effects") or []:
                if (
                    effect.get("type") == "set_variable"
                    and effect.get("variable") == "project_policy"
                ):
                    values.append(effect["value"])
    return values


def _fleet_policy() -> dict[str, Any]:
    writes = [value for value in _project_policy_writes() if "fleet_health" in value]
    assert len(writes) == 1
    return writes[0]


def test_shipped_policy_carries_the_fleet_health_defaults() -> None:
    assert _fleet_policy()["fleet_health"] == _DEFAULTS


def test_shipped_rules_no_longer_hold_on_the_five_minute_load() -> None:
    for path in sorted(_RULES_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        assert "loads>60" not in text, path
        assert "two consecutive 5-minute loads" not in text, path
