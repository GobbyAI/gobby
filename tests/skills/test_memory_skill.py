"""Memory guidance keeps search-first and durable-knowledge boundaries."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/gobby/references/memory"
)


def test_memory_skill_is_search_first() -> None:
    content = " ".join((ROOT / "overview.md").read_text().split())
    assert (
        "Search the task subject before editing claimed work, before unfamiliar subsystem work, and before capture"
        in content
    )
    assert "never read or write provider-native memory files" in content
    assert "Most turns and completed tasks need no memory write" in content
    search = " ".join((ROOT / "search.md").read_text().split())
    assert "content and rationale" in search
    assert "`similarity` includes temporal decay" in search
    assert "`min_score` filters `undecayed_similarity`" in search
    assert "Treat hits as evidence, not authority" in search


def test_memory_skill_routes_plan_drafts_to_plan_artifacts() -> None:
    content = " ".join((ROOT / "overview.md").read_text().split())
    assert "plans/evidence for proposals and findings" in content
    assert "Fix found bugs in the current task" in content
    assert "repository's found-work ladder" in content
    assert "do not store them as memories or create another task merely to defer them" in content
    capture = " ".join((ROOT / "capture.md").read_text().split())
    assert "Search first" in capture
    assert "explicit durable remember request or non-obvious knowledge" in capture
    assert "one-time instruction into a permanent preference" in capture
