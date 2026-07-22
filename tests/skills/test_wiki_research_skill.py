"""Contract tests for the bundled wiki-research skill's task-filing triage."""

from pathlib import Path

from gobby.skills.parser import parse_skill_file

SKILL_PATH = Path("src/gobby/install/shared/skills/wiki-research/SKILL.md")


def test_investigation_tasks_require_triage_before_filing() -> None:
    """Step 8 must dedup against the task graph and codebase before create_task."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert parsed.name == "wiki-research"

    assert "## 8. Investigation tasks (only when `create_tasks`)" in body
    section_8 = body.split("## 8. Investigation tasks")[1].split("## 9.")[0]

    # Task-graph dedup: BM25 search surfaces open AND closed duplicates.
    assert "`search_tasks` on `gobby-tasks`" in section_8
    assert "do not re-file; its close rationale is the answer" in section_8
    # Prior-art check against the codebase.
    assert "gcode search" in section_8
    assert "gcode search-content" in section_8
    # Near-misses are filed with cross-references, not dropped.
    assert "related: #NNNN" in section_8
    # Filing happens only for survivors, and skips are never silent.
    assert "per surviving item" in section_8
    assert "Skipped filings are visible, never silent." in section_8


def test_run_report_records_triaged_away_items() -> None:
    """Step 9 must produce a reachable report without writing generated surfaces."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    section_9 = body.split("## 9. Run report")[1].split("## 10.")[0]
    assert "items triaged away in step 8" in section_9
    assert "duplicate task refs or prior-art reasons" in section_9
    assert "`wiki_ingest`" in section_9
    assert "local path" in section_9
    assert "vault source under `raw/`" in section_9
    assert "`outputs/**` reserved for generated artifacts" in section_9
    assert "`wiki_compile` records" in section_9
    assert "Append one line" not in section_9
