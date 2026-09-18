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
        "search the task subject before editing claimed work, before unfamiliar subsystem work,"
        " before characterizing provider or runtime behavior, and before capture" in content
    )
    assert "never read or write provider-native memory files" in content
    assert "Most turns and completed tasks need no memory write" in content
    search = " ".join((ROOT / "search.md").read_text().split())
    assert "content and rationale" in search
    assert "`similarity` includes temporal decay" in search
    assert "`min_score` filters `undecayed_similarity`" in search
    assert "Treat hits as evidence, not authority" in search


def test_memory_skill_describes_the_pushed_index() -> None:
    content = " ".join((ROOT / "overview.md").read_text().split())
    assert "Gobby may push a `<memory-index>` of ranked one-line hits" in content
    assert "Each line ends in a `when:` clause drawn from the memory's rationale" in content
    assert "with `get_memory` before acting, even when the code is familiar" in content
    assert "Write the rationale as the `when:` clause a future session would match" in content
    search = " ".join((ROOT / "search.md").read_text().split())
    assert "The closing `when:` clause is the lead of the memory's rationale" in search
    assert "it never replaces your own search" in search


def test_memory_skill_reviews_after_task_close() -> None:
    post_task = " ".join((ROOT / "post-task.md").read_text().split())
    assert "Load after the post-close prompt for a worked leaf" in post_task
    assert "`gobby-memory:review_task_memories(task_id, changes_summary)`" in post_task
    assert "most tasks need no write" in post_task


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
