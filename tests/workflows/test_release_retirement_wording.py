"""Shipped seat wording names the CLI restart path, not the retired release batch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
TRIAGE = SHARED / "workflows/agents/triage.yaml"
NO_DAEMON_MANAGEMENT = SHARED / "workflows/rules/worker-safety/no-daemon-management.yaml"


def test_triage_digest_queues_restart_outcomes() -> None:
    digest = next(line for line in TRIAGE.read_text().splitlines() if "**Digest.**" in line)

    assert "restart outcomes" in digest
    assert "release batch" not in digest.lower()


def test_daemon_management_rule_names_the_orchestrator_restart() -> None:
    rules = yaml.safe_load(NO_DAEMON_MANAGEMENT.read_text())["rules"]
    description = rules["no-daemon-management"]["description"]

    assert "the Orchestrator restarts it after a global notice" in description
    assert "release batch" not in description.lower()
