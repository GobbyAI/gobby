from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.definitions import AgentDefinitionBody, validate_workflow_definition_data

pytestmark = pytest.mark.unit


def _bundled_agent_files() -> list[Path]:
    files = sorted(get_bundled_agents_path().glob("*.yaml"))
    # Keep this loose so adding or removing bundled agents does not require
    # updating a brittle magic number in the test.
    assert len(files) >= 10, f"expected bundled agents, got {len(files)}: {[f.name for f in files]}"
    return files


@pytest.mark.parametrize("path", _bundled_agent_files(), ids=lambda path: path.name)
def test_bundled_agent_yaml_validates(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    assert validate_workflow_definition_data(data, expected_type="agent") == "agent"
    body = AgentDefinitionBody.model_validate(data)

    assert body.name == path.stem
    if body.supports_surface("spawn"):
        assert body.prompts.agent is not None
    if body.supports_surface("persona"):
        assert body.prompts.persona is not None


def test_bundled_persona_prompts_exclude_agent_lifecycle_language() -> None:
    forbidden = (
        "assigned_task_id",
        "submit_for_review",
        "close_task",
        "end_agent_run",
        "send_message",
    )

    for path in _bundled_agent_files():
        body = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
        if not body.supports_surface("persona"):
            continue
        persona = body.prompt_for("persona") or ""
        for term in forbidden:
            assert term not in persona, f"{path.name} persona contains agent lifecycle term: {term}"


def test_all_bundled_agents_include_semantic_lifecycle_note() -> None:
    required_terms = ("turn_start", "turn_end", "before_agent", "after_agent", "stop")

    for path in _bundled_agent_files():
        body = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
        if not body.supports_surface("spawn"):
            continue
        instructions = body.prompt_for("agent") or ""

        for term in required_terms:
            assert term in instructions, f"{path.name} is missing lifecycle term: {term}"


def test_ambiguous_stop_phrases_are_removed_from_bundled_agents() -> None:
    banned_phrases = (
        'say "no action needed" and stop',
        "last stop before merge",
        "do not stop or exit without calling kill_agent",
        "do not stop without calling kill_agent",
    )

    for path in _bundled_agent_files():
        body = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
        if not body.supports_surface("spawn"):
            continue
        instructions = body.prompt_for("agent").lower()

        for phrase in banned_phrases:
            assert phrase not in instructions, f"{path.name} still contains: {phrase}"
