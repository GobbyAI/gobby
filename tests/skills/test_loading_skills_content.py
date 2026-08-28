"""Tests for complete skill-delivery guidance in the loading-skills bundle."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_loading_skills_requires_complete_separate_results_and_retry() -> None:
    skill_path = (
        Path(__file__).parents[2]
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "skills"
        / "loading-skills"
        / "SKILL.md"
    )
    content = skill_path.read_text(encoding="utf-8")

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
