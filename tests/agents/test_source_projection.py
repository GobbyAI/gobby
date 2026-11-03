"""Synthetic documents exercise configurable evidence projection mechanics."""

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.source_projection import (
    main,
    page_projection,
    project_markdown,
    select_source,
    task_refs,
)


def test_task_refs_expand_deduplicate_and_ignore_qualified_session_refs() -> None:
    assert task_refs("#12–#14 then #12; workspace#19") == ["#12", "#13", "#14"]
    with pytest.raises(ValueError, match="range"):
        task_refs("#8-#3")
    with pytest.raises(ValueError, match="range"):
        task_refs("#1-#999")


def test_table_projection_uses_caller_columns_and_row_filter() -> None:
    spec = yaml.safe_load("""
mode: table
headings: [Pending exports]
columns: {key: 0, refs: 2}
ref_columns: [refs]
row_pattern: '^batch-'
""")
    content = """## Pending exports
| Key | Ignored | Tasks |
| --- | --- | --- |
| batch-a | invisible | #7-#9 |
| batch-b | private\\|cell | #22 |
## Other
| batch-c | unseen | #99 |
"""
    result = project_markdown(content, spec)
    assert result == {
        "entries": [
            {"key": "batch-a", "refs": ["#7", "#8", "#9"]},
            {"key": "batch-b", "refs": ["#22"]},
        ],
        "gaps": [],
    }


def test_topics_and_decisions_use_configured_headings_and_patterns() -> None:
    content = """## Activity — today
- build pending Earlier: old hold
- build ready
- deploy waiting
## Questions
- Pick region
- Pick date
"""
    spec: dict[str, Any] = {
        "mode": "topics",
        "heading_prefixes": ["Activity —"],
        "topics": {"compile": "build", "release": "deploy"},
        "trim_marker": "Earlier:",
        "last": 1,
    }
    assert project_markdown(content, spec) == {
        "compile": ["- build ready"],
        "release": ["- deploy waiting"],
        "gaps": [],
    }
    result = project_markdown(content, {"mode": "bullets", "headings": ["Questions", "Absent"]})
    assert result == {
        "entries": [
            {"heading": "Questions", "text": "- Pick region"},
            {"heading": "Questions", "text": "- Pick date"},
        ],
        "gaps": ["Missing heading: Absent"],
    }


def test_projection_pages_complete_unicode_entries_within_envelope_budget() -> None:
    projection = {"entries": ["é" * 80, "ø" * 80, "x" * 80]}
    first = page_projection(projection, {"source": "fixture.md"}, 0, budget=370)
    assert first["count"] == 1
    assert first["next_offset"] == 1
    assert len(json.dumps(first, ensure_ascii=False).encode()) + 1 < 370
    second = page_projection(projection, {"source": "fixture.md"}, first["next_offset"], 370)
    assert second["evidence"]["entries"][0] == projection["entries"][1]
    with pytest.raises(ValueError, match="budget"):
        page_projection({"entries": ["x" * 500]}, {}, 0, budget=100)
    with pytest.raises(ValueError, match="nonnegative"):
        page_projection(projection, {}, -1)


def test_dated_source_prefers_today_then_latest_prior_and_ignores_future(tmp_path: Path) -> None:
    for name in ["brief-2026-02-01.md", "brief-2026-02-03.md", "brief-2026-02-09.md"]:
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    pattern = r"brief-(\d{4}-\d{2}-\d{2})"
    selected_date, path = select_source(tmp_path, "brief-*.md", pattern, date(2026, 2, 3))
    assert selected_date == date(2026, 2, 3)
    assert path.name == "brief-2026-02-03.md"
    selected_date, path = select_source(tmp_path, "brief-*.md", pattern, date(2026, 2, 5))
    assert selected_date == date(2026, 2, 3)
    assert path.name == "brief-2026-02-03.md"


def test_unknown_projection_mode_is_explicit_error() -> None:
    with pytest.raises(ValueError, match="mode"):
        project_markdown("## Sample\nhello", {"mode": "invalid"})


def test_cli_reads_yaml_spec_and_reports_stale_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "brief-2001-01-01.md").write_text("## Items\n- fixture evidence\n")
    spec = r"""
source_glob: brief-*.md
date_pattern: 'brief-(\d{4}-\d{2}-\d{2})'
sections:
  sample:
    mode: bullets
    headings: [Items]
"""
    monkeypatch.setattr("gobby.agents.source_projection.find_project_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["projection", "--spec", spec, "--section", "sample"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["source_date"] == "2001-01-01"
    assert result["stale"] is True
    assert result["evidence"]["entries"] == [{"heading": "Items", "text": "- fixture evidence"}]
    assert result["next_offset"] is None
