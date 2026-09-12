"""Tests for complete skill-delivery guidance in the loading-skills bundle."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SKILL_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "loading-skills"
    / "SKILL.md"
)


def test_loading_skills_requires_complete_separate_results_and_retry() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")

    assert "one request for one page per outer tool result" in content
    assert "loaded only after the final entrypoint page" in content
    assert "deduplicate names" in content
    assert "sequentially in required order" in content
    assert "Do not use `Promise.all`" in content
    assert "`brief=true` by default" in content
    assert "only `cursor=<opaque cursor>`" in content
    assert "`page.next_cursor` is null" in content
    assert "current page's `content` together with `page`" in content
    assert "use its topic index to select references" in content
    assert '`get_skill_file(name="<skill>", path="references/<topic>.md")`' in content
    assert "`…N tokens truncated…`" in content
    assert "restart that skill or file lookup individually" in content
    assert "Collapsed UI previews are presentation-only" in content


def test_loading_skills_states_schema_first_reference_loads() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")

    assert "`get_skill_file` and `get_skill_files` are not bootstrap tools" in content
    assert '`get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file")`' in content
    assert "after the schema lease above, via its exact" in content
