"""The Assistant owns the scheduled status; the status-report tick wakes it through a pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
ASSISTANT = SHARED / "workflows/agents/assistant.yaml"
PROCEDURE = SHARED / "skills/gobby/references/agents/procedures/assistant.md"
WAKE = SHARED / "workflows/pipelines/assistant-status-wake.yaml"


def _assistant_status_prompt() -> str:
    data: dict[str, Any] = yaml.safe_load(ASSISTANT.read_text(encoding="utf-8"))
    prompt = data["prompts"]["agent"]
    assert isinstance(prompt, str)
    return prompt


def test_assistant_definition_owns_status_at_policy_cadence() -> None:
    prompt = _assistant_status_prompt()

    assert "project_policy.report_cadence_seconds" in prompt
    assert "send_operator_message" in prompt
    assert "assistant.md" in prompt
    assert "Reporter owns" not in prompt
    assert "Don't assemble status yourself" not in prompt


def test_assistant_definition_keeps_attached_pane_mirroring_rule() -> None:
    prompt = _assistant_status_prompt()

    assert "automatic mirroring" in prompt


def test_status_procedure_reads_cadence_from_policy_without_literal() -> None:
    text = PROCEDURE.read_text(encoding="utf-8")

    assert "project_policy" in text
    assert "report_cadence_seconds" in text
    assert "send_operator_message" in text
    assert "US Central" in text
    assert "1800" not in text


def test_status_procedure_deliver_takes_one_path_per_tick() -> None:
    deliver = PROCEDURE.read_text(encoding="utf-8").split("## Deliver", 1)[1]

    assert "list_conversation_attachments" in deliver
    assert "- Attached: type the whole markdown report once" in deliver
    assert "Do not call `send_operator_message` for this tick" in deliver
    assert "- Not attached: send the whole markdown report once" in deliver
    assert "Type nothing in this pane for this tick" in deliver


def test_wake_pipeline_has_no_cadence_literal() -> None:
    assert "1800" not in WAKE.read_text(encoding="utf-8")


def test_wake_pipeline_resolves_each_live_status_then_wakes_that_session() -> None:
    data: dict[str, Any] = yaml.safe_load(WAKE.read_text(encoding="utf-8"))

    steps = {step["id"]: step for step in data["steps"]}
    resolve_ids = [step_id for step_id in steps if step_id.startswith("resolve_")]
    assert list(steps) == [*resolve_ids, "wake"]

    statuses: list[str] = []
    for step_id in resolve_ids:
        resolve = steps[step_id]["mcp"]
        assert (resolve["server"], resolve["tool"]) == ("gobby-sessions", "list_sessions")
        assert resolve["arguments"]["project_id"] == "${{ project_id }}"
        assert resolve["arguments"]["agent_type"] == "${{ inputs.agent_type }}"
        statuses.append(resolve["arguments"]["status"])
    assert statuses == [
        "active",
        "paused",
        "interrupted",
        "awaiting_input",
        "awaiting_approval",
        "awaiting_handoff",
    ]

    wake = steps["wake"]["mcp"]
    assert (wake["server"], wake["tool"]) == ("gobby-agents", "send_message")
    assert wake["arguments"]["target"] == "session"
    assert wake["arguments"]["wake"] is True
    for step_id in resolve_ids:
        assert f"steps.{step_id}.output" in steps["wake"]["condition"]

    assert data["inputs"]["agent_type"]["default"] == "assistant"
