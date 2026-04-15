from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


def _bundled_agent_files() -> list[Path]:
    files = sorted(get_bundled_agents_path().glob("*.yaml"))
    assert len(files) == 13, (
        f"expected 13 bundled agents, got {len(files)}: "
        f"{[f.name for f in files]}"
    )
    return files


@pytest.mark.parametrize("path", _bundled_agent_files(), ids=lambda path: path.name)
def test_bundled_agent_yaml_validates(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    body = AgentDefinitionBody.model_validate(data)

    assert body.name == path.stem
    assert body.instructions is not None


def test_all_bundled_agents_include_semantic_lifecycle_note() -> None:
    required_terms = ("turn_start", "turn_end", "before_agent", "after_agent", "stop")

    for path in _bundled_agent_files():
        data = yaml.safe_load(path.read_text())
        instructions = str(data.get("instructions") or "")

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
        data = yaml.safe_load(path.read_text())
        instructions = str(data.get("instructions") or "").lower()

        for phrase in banned_phrases:
            assert phrase not in instructions, f"{path.name} still contains: {phrase}"
