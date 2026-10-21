"""Surviving planning and review carriers retain their reference contracts."""

import re
from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


def test_reference_contract_4_2_2() -> None:
    """Planning/build/review carriers preserve obligations and point to shipped topics."""
    shared = Path("src/gobby/install/shared")
    expected = {
        "plan-adversary": ("plan/review", "review"),
        "epic-reviewer": ("review/epic", "Discovery Brief"),
        "developer": ("development/obligations", "test-driven-development"),
        "default": ("skills/loading", "gobby-skills"),
    }
    for name, (topic, obligation) in expected.items():
        path = shared / "workflows" / "agents" / f"{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        agent = AgentDefinitionBody.model_validate(data)
        text = path.read_text(encoding="utf-8")
        assert f"references/{topic}.md" in text, name
        assert obligation in (agent.prompt_for("agent") or ""), name
        for reference in re.findall(r"references/[a-z-]+/[a-z-]+\.md", text):
            assert (shared / "skills" / "gobby" / reference).is_file(), (name, reference)
    for filename in ("system.md", "user.md"):
        prompt = (shared / "prompts" / "expansion" / filename).read_text(encoding="utf-8")
        assert "gobby:references/plan/expansion.md" in prompt
        assert "additional_skills" in prompt
        assert "get_skill_file" in prompt and "get_tool_schema" in prompt
        assert "page.next_cursor" in prompt
        assert "assigned_agent" in prompt
        assert "expansion-agent-selection" not in prompt
