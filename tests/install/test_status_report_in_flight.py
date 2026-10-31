from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

PROCEDURES = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/skills/gobby/references/agents/procedures"
)
ASSISTANT = PROCEDURES / "assistant.md"
REPORTER = PROCEDURES / "reporter.md"
FULL_REPORT = PROCEDURES / "reporter-full-report.md"
IN_FLIGHT_HEADING = "In-flight tasks: stage, time in stage, claim-to-close"
ROOT_REGEX = "^(24818)\\."
TIP = "1" * 40
STALE = "2" * 40
EXPECTED_STAGES = {
    "test rejected": ("test-writing", "decisive step record"),
    "test approved": ("development", "decisive step record"),
    "impl approved": ("review", "decisive step record"),
    "review approved": ("waiting to land", "decisive step record"),
    "review stale": ("unknown", "decisive step record"),
    "impl rejected": ("development", "decisive step record"),
    "review rejected": ("development", "decisive step record"),
    "old epoch": ("development", "decisive step record"),
    "supplemental only": ("review", "decisive step record"),
    "legacy review": ("review", "decisive step record"),
    "claim only": ("development", "claim time"),
    "test pending": ("test-writing", "first step record"),
    "unclaimed impl step": ("development", "first step record"),
}


def _section(text: str, heading: str) -> str:
    """Return a level-2 section's body, up to the next level-2 heading."""
    _, found, rest = text.partition(f"\n## {heading}\n")
    assert found, f"missing section: {heading}"
    return rest.split("\n## ", 1)[0]


def _bullet(section: str, label: str) -> str:
    """Return one top-level bullet, from its label to the next bullet."""
    _, found, rest = section.partition(f"\n- {label}:")
    assert found, f"missing bullet: {label}"
    return rest.split("\n- ", 1)[0]


def _recipe_queries() -> tuple[str, str]:
    """Return the stage and closure statements of the in-flight recipe."""
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)
    blocks = [block.split("```", 1)[0].strip() for block in recipe.split("```sql\n")[1:]]
    assert len(blocks) == 2
    return blocks[0], blocks[1]


def _read(db: HubDatabase, sql: str, params: tuple[Any, ...], limit: int = 2000) -> list[Any]:
    """Run a recipe statement the way query_readonly does, under its outer LIMIT."""
    with db.transaction() as conn:
        rows = conn.execute(
            f"SELECT * FROM (\n{sql}\n) AS readonly_result LIMIT %s", (*params, limit)
        ).fetchall()
    return [dict(row) for row in rows]


def _execute(db: HubDatabase, sql: str, params: tuple[Any, ...]) -> None:
    with db.transaction() as conn:
        conn.execute(sql, params)


def _project(db: HubDatabase) -> str:
    project_id = str(uuid.uuid4())
    _execute(
        db,
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, NOW())",
        (project_id, f"status-{project_id}"),
    )
    return project_id


def _session(session_manager: SessionManager, project_id: str) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    return session_manager.register(
        external_id=f"status-{uuid.uuid4()}",
        machine_id=machine_id,
        source="claude",
        project_id=project_id,
    ).id


def _task(db: HubDatabase, project_id: str, title: str, path: str) -> str:
    task_id = (
        LocalTaskManager(db).create_task(project_id, title, validation_criteria="Stage rows.").id
    )
    _execute(db, "UPDATE tasks SET path_cache = %s WHERE id = %s", (path, task_id))
    return task_id


def _claim(db: HubDatabase, task_id: str, session_id: str) -> None:
    _execute(db, "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s", (session_id, task_id))


def _link(db: HubDatabase, task_id: str, *shas: str) -> None:
    _execute(db, "UPDATE tasks SET commits = %s::jsonb WHERE id = %s", (json.dumps(shas), task_id))


def _close(db: HubDatabase, task_id: str, closed_at: datetime) -> None:
    _execute(
        db,
        "UPDATE tasks SET closed_at = %s, closed_reason = 'done' WHERE id = %s",
        (closed_at, task_id),
    )


def _step(
    db: HubDatabase,
    task_id: str,
    session_id: str,
    sequence: int,
    step: str,
    status: str,
    *,
    epoch: int = 1,
    commit: str | None = None,
    supplemental: bool | None = None,
) -> None:
    body = {
        "step": step,
        "status": status,
        "sequence": sequence,
        "epoch": epoch,
        "round_number": 1,
        "feedback": "seeded",
        "commit_sha": commit,
        "supplemental_review": supplemental,
    }
    _execute(
        db,
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'task_step', %s)",
        (str(uuid.uuid4()), task_id, session_id, json.dumps(body)),
    )


def _seed_in_flight(
    db: HubDatabase, session_manager: SessionManager, project_id: str, other_project_id: str
) -> None:
    holder = _session(session_manager, project_id)

    def add(title: str, path: str = "24818.1", project: str = project_id) -> str:
        return _task(db, project, title, path)

    rejected = add("test rejected")
    _claim(db, rejected, holder)
    _step(db, rejected, holder, 1, "test", "rejected")

    approved = add("test approved")
    _claim(db, approved, holder)
    _step(db, approved, holder, 1, "test", "approved")

    review = add("impl approved")
    _claim(db, review, holder)
    _link(db, review, TIP)
    _step(db, review, holder, 1, "test", "approved")
    _step(db, review, holder, 2, "implementation", "approved", commit=TIP)

    landing = add("review approved")
    _claim(db, landing, holder)
    _link(db, landing, TIP)
    _step(db, landing, holder, 1, "test", "approved")
    _step(db, landing, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, landing, holder, 3, "review", "approved", commit=TIP, supplemental=False)

    stale = add("review stale")
    _claim(db, stale, holder)
    _link(db, stale, TIP)
    _step(db, stale, holder, 1, "test", "approved")
    _step(db, stale, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, stale, holder, 3, "review", "approved", commit=STALE, supplemental=False)

    impl_rejected = add("impl rejected")
    _claim(db, impl_rejected, holder)
    _link(db, impl_rejected, TIP)
    _step(db, impl_rejected, holder, 1, "test", "approved")
    _step(db, impl_rejected, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, impl_rejected, holder, 3, "implementation", "rejected")

    review_rejected = add("review rejected")
    _claim(db, review_rejected, holder)
    _link(db, review_rejected, TIP)
    _step(db, review_rejected, holder, 1, "test", "approved")
    _step(db, review_rejected, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, review_rejected, holder, 3, "review", "rejected", commit=TIP, supplemental=False)

    old_epoch = add("old epoch")
    _claim(db, old_epoch, holder)
    _link(db, old_epoch, TIP)
    _execute(db, "UPDATE tasks SET review_epoch = 2 WHERE id = %s", (old_epoch,))
    _step(db, old_epoch, holder, 1, "test", "approved")
    _step(db, old_epoch, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, old_epoch, holder, 3, "review", "approved", commit=TIP, supplemental=False)
    _step(db, old_epoch, holder, 4, "test", "approved", epoch=2)

    supplemental = add("supplemental only")
    _claim(db, supplemental, holder)
    _link(db, supplemental, TIP)
    _step(db, supplemental, holder, 1, "test", "approved")
    _step(db, supplemental, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, supplemental, holder, 3, "review", "approved", commit=TIP, supplemental=True)

    legacy = add("legacy review")
    _claim(db, legacy, holder)
    _link(db, legacy, TIP)
    _step(db, legacy, holder, 1, "test", "approved")
    _step(db, legacy, holder, 2, "implementation", "approved", commit=TIP)
    _step(db, legacy, holder, 3, "review", "approved", commit=TIP)

    claim_only = add("claim only")
    _claim(db, claim_only, holder)

    pending = add("test pending")
    _claim(db, pending, holder)
    _step(db, pending, holder, 1, "test", "in_progress")

    unclaimed = add("unclaimed impl step")
    _step(db, unclaimed, holder, 1, "implementation", "pending")

    add("idle")

    closed = add("closed")
    _claim(db, closed, holder)
    _close(db, closed, datetime.now(UTC))

    add("other root", path="30000.1")

    other = _session(session_manager, other_project_id)
    elsewhere = add("other project", project=other_project_id, path="24818.9")
    _claim(db, elsewhere, other)


def test_assistant_full_and_delta_reports_require_in_flight_section() -> None:
    report = _section(ASSISTANT.read_text(encoding="utf-8"), "Report")

    assert "in-flight" in _bullet(report, "Full")
    assert "claim-to-close" in _bullet(report, "Full")
    assert "in-flight" in _bullet(report, "Delta")
    assert "claim-to-close" in _bullet(report, "Delta")


def test_delta_reports_carry_the_complete_in_flight_list() -> None:
    report = _section(ASSISTANT.read_text(encoding="utf-8"), "Report")

    assert "complete" in _bullet(report, "Delta")
    assert "unchanged" in _bullet(report, "Delta")
    reporter_delta = _section(REPORTER.read_text(encoding="utf-8"), "Delta report")
    assert "complete" in reporter_delta
    assert "unchanged" in reporter_delta


def test_full_report_scaffold_has_in_flight_slot() -> None:
    scaffold = FULL_REPORT.read_text(encoding="utf-8")

    assert "- In-flight tasks:" in scaffold
    assert "claim-to-close" in scaffold


def test_assistant_gather_names_the_in_flight_query_owner() -> None:
    gather = ASSISTANT.read_text(encoding="utf-8")

    assert IN_FLIGHT_HEADING in gather
    assert "run the two in-flight queries with `query_readonly`" in gather


def test_in_flight_recipe_names_query_seat_and_stage_sources() -> None:
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)

    assert "`query_readonly` is Assistant- and Orchestrator-only" in recipe
    assert "A Reporter seat cannot run them" in recipe
    assert "task_claim_intervals" in recipe
    assert "author_type = 'task_step'" in recipe
    assert "#25377" in recipe
    assert "limit=2000" in recipe


def test_in_flight_queries_are_single_ascii_read_only_statements() -> None:
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)
    blocks = recipe.split("```sql\n")[1:]

    assert len(blocks) == 2
    for block in blocks:
        statement = block.split("```", 1)[0].strip()
        assert statement.startswith(("SELECT", "WITH"))
        assert ";" not in statement
        assert statement.isascii()
        assert "t.project_id = %s" in statement
        assert "count(*) OVER ()" in statement
        assert "seq_num > %s" in statement


def test_in_flight_stage_follows_current_epoch_and_primary_records(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    other_project_id = _project(temp_db)
    _seed_in_flight(temp_db, session_manager, sample_project["id"], other_project_id)
    stage_sql, _ = _recipe_queries()

    rows = _read(temp_db, stage_sql, (sample_project["id"], ROOT_REGEX, 0))

    stages = {row["title"]: (row["stage"], row["stage_basis"]) for row in rows}
    assert stages == EXPECTED_STAGES
    assert {row["total"] for row in rows} == {len(EXPECTED_STAGES)}


def test_in_flight_pages_keep_the_full_total_under_the_outer_limit(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    other_project_id = _project(temp_db)
    _seed_in_flight(temp_db, session_manager, sample_project["id"], other_project_id)
    stage_sql, _ = _recipe_queries()

    first = _read(temp_db, stage_sql, (sample_project["id"], ROOT_REGEX, 0), limit=3)
    assert len(first) == 3
    assert first[0]["total"] == len(EXPECTED_STAGES)

    rest = _read(temp_db, stage_sql, (sample_project["id"], ROOT_REGEX, first[-1]["seq_num"]))
    titles = [row["title"] for row in first + rest]
    assert len(titles) == len(set(titles)) == len(EXPECTED_STAGES)


def test_closures_report_claim_to_close_inside_the_window(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    other_project_id = _project(temp_db)
    holder = _session(session_manager, project_id)

    claimed = _task(temp_db, project_id, "closed claimed", "24818.2")
    _claim(temp_db, claimed, holder)
    _close(temp_db, claimed, datetime.now(UTC))

    untimed = _task(temp_db, project_id, "closed untimed", "24818.3")
    _close(temp_db, untimed, datetime.now(UTC))

    earlier = _task(temp_db, project_id, "closed earlier", "24818.4")
    _close(temp_db, earlier, datetime.now(UTC) - timedelta(hours=2))

    elsewhere = _task(temp_db, other_project_id, "closed other project", "24818.5")
    _claim(temp_db, elsewhere, _session(session_manager, other_project_id))
    _close(temp_db, elsewhere, datetime.now(UTC))

    outside = _task(temp_db, project_id, "closed other root", "30000.1")
    _close(temp_db, outside, datetime.now(UTC))

    _, closure_sql = _recipe_queries()
    window_start = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    rows = _read(temp_db, closure_sql, (project_id, window_start, ROOT_REGEX, 0))

    by_title = {row["title"]: row for row in rows}
    assert set(by_title) == {"closed claimed", "closed untimed"}
    assert by_title["closed claimed"]["first_claimed_at"] is not None
    assert by_title["closed claimed"]["claim_to_close"] is not None
    assert by_title["closed untimed"]["first_claimed_at"] is None
    assert {row["total"] for row in rows} == {2}
