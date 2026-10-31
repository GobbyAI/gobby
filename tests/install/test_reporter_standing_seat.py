"""The Reporter is a standing seat: it yields between ticks and wakes on the policy cadence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.agent_models import AgentDefinitionBody

pytestmark = pytest.mark.unit

SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
REPORTER = SHARED / "workflows/agents/reporter.yaml"
PROCEDURE = SHARED / "skills/gobby/references/agents/procedures/reporter.md"


def _reporter() -> AgentDefinitionBody:
    data: dict[str, Any] = yaml.safe_load(REPORTER.read_text(encoding="utf-8"))
    return AgentDefinitionBody.model_validate(data)


def _render(**policy: Any) -> str:
    return _reporter().prompt_for("agent", variables={"project_policy": {"roots": [], **policy}})


def test_reporter_definition_is_an_interactive_standing_seat() -> None:
    body = _reporter()

    assert body.execution_mode == "interactive"
    assert body.idle_ttl_seconds is None
    assert body.timeout == 0


def test_reporter_prompt_waits_on_policy_cadence_between_ticks() -> None:
    prompt = _render(report_cadence_seconds=1800)

    assert "gobby-agents:wait_for_coordination(reply=true, timeout=1800)" in prompt
    assert "Its timeout expiry wakes you for the next tick" in prompt
    assert "End with `gobby-agents:end_agent_run`" not in prompt
    assert "one-shot" not in prompt


def test_reporter_prompt_without_cadence_yields_until_directed() -> None:
    prompt = _render()

    assert "timeout=None" not in prompt
    assert "gobby-agents:wait_for_coordination(reply=true" not in prompt
    assert "until directed" in prompt


def test_reporter_procedure_ends_each_tick_by_yielding_not_ending_the_run() -> None:
    procedure = PROCEDURE.read_text(encoding="utf-8")

    assert "End with `gobby-agents:end_agent_run`" not in procedure
    assert "900-second" not in procedure
    assert "report_cadence_seconds" in procedure
