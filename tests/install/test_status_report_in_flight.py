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
from gobby.tasks.attestations import EvidenceReference, TaskAttestation
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
IN_FLIGHT_HEADING = "In-flight tasks: evidence activity and claim-to-close"
ROOT_REGEX = "^(24818)\\."
TIP = "1" * 40
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LATEST_AT = T0 + timedelta(minutes=5)
EVIDENCE_COLUMNS = {
    "seq_num",
    "title",
    "claimed_by_session_id",
    "evidence_kind",
    "evidence_result",
    "evidence_summary",
    "evidence_reference",
    "evidence_recorded_at",
    "first_claimed_at",
    "total",
}
EXPECTED_EVIDENCE = {
    "claim only": (None, None),
    "legacy steps only": (None, None),
    "latest wins": ("test", "approved"),
    "correction": ("review", "rejected"),
    "unclaimed attested": ("test", "approved"),
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
    """Return the evidence-activity and claim-to-close statements of the in-flight recipe."""
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)
    blocks = [block.split("```", 1)[0].strip() for block in recipe.split("```sql\n")[1:]]
    assert len(blocks) == 2
    return blocks[0], blocks[1]


def _read(db: HubDatabase, sql: str, params: tuple[Any, ...], limit: int = 2000) -> list[Any]:
    """Run a recipe statement the way query_readonly runs it: a REPEATABLE READ READ ONLY
    snapshot under the outer LIMIT."""
    with db.bounded_transaction(repeatable_read_read_only=True) as conn:
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


def _close(db: HubDatabase, task_id: str, closed_at: datetime) -> None:
    _execute(
        db,
        "UPDATE tasks SET closed_at = %s, closed_reason = 'done' WHERE id = %s",
        (closed_at, task_id),
    )


def _legacy_step(
    db: HubDatabase, task_id: str, session_id: str, sequence: int, step: str, status: str
) -> None:
    """Seed a retired task_step record, which the evidence report must not read as stage."""
    body = {
        "step": step,
        "status": status,
        "sequence": sequence,
        "epoch": 1,
        "round_number": 1,
        "feedback": "seeded",
        "commit_sha": TIP,
    }
    _execute(
        db,
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'task_step', %s)",
        (str(uuid.uuid4()), task_id, session_id, json.dumps(body)),
    )


def _attest(
    db: HubDatabase,
    task_id: str,
    session_id: str,
    sequence: int,
    kind: str,
    result: str,
    *,
    summary: str,
    recorded_at: datetime,
    reference: EvidenceReference | None = None,
    supersedes_id: str | None = None,
) -> str:
    """Store an attestation in the writer's shape, validated by its model."""
    record = TaskAttestation(
        id=str(uuid.uuid4()),
        task_id=task_id,
        sequence=sequence,
        session_id=session_id,
        agent_definition="seeded",
        agent_definition_hash="0" * 64,
        agent_capabilities=(),
        created_at=recorded_at,
        kind=kind,
        result=result,
        summary=summary,
        evidence={},
        reference=reference,
        supersedes_id=supersedes_id,
    )
    _execute(
        db,
        "INSERT INTO task_comments (id, task_id, author, author_type, body, created_at) "
        "VALUES (%s, %s, %s, 'task_attestation', %s, %s)",
        (record.id, task_id, session_id, record.model_dump_json(), recorded_at),
    )
    return record.id


def _seed_in_flight(
    db: HubDatabase, session_manager: SessionManager, project_id: str, other_project_id: str
) -> None:
    holder = _session(session_manager, project_id)

    def add(title: str, path: str = "24818.1", project: str = project_id) -> str:
        return _task(db, project, title, path)

    claim_only = add("claim only")
    _claim(db, claim_only, holder)

    legacy = add("legacy steps only")
    _claim(db, legacy, holder)
    _legacy_step(db, legacy, holder, 1, "test", "approved")
    _legacy_step(db, legacy, holder, 2, "implementation", "approved")
    _legacy_step(db, legacy, holder, 3, "review", "approved")

    latest = add("latest wins")
    _claim(db, latest, holder)
    _attest(db, latest, holder, 1, "test", "rejected", summary="first run failed", recorded_at=T0)
    _attest(
        db,
        latest,
        holder,
        2,
        "test",
        "approved",
        summary="rerun green",
        recorded_at=LATEST_AT,
        reference=EvidenceReference(scheme="commit", value=TIP),
    )

    corrected = add("correction")
    _claim(db, corrected, holder)
    original = _attest(
        db, corrected, holder, 1, "review", "approved", summary="premature", recorded_at=T0
    )
    _attest(
        db,
        corrected,
        holder,
        2,
        "review",
        "rejected",
        summary="reviewer objected",
        recorded_at=T0 + timedelta(minutes=1),
        supersedes_id=original,
    )

    attested = add("unclaimed attested")
    _attest(db, attested, holder, 1, "test", "approved", summary="ran unclaimed", recorded_at=T0)

    add("idle")

    closed = add("closed")
    _claim(db, closed, holder)
    _attest(db, closed, holder, 1, "test", "approved", summary="closed work", recorded_at=T0)
    _close(db, closed, datetime.now(UTC))

    outside = add("other root", path="30000.1")
    _claim(db, outside, holder)

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


def test_in_flight_recipe_names_query_seat_and_sources() -> None:
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)

    assert "Use `query_readonly` only when your installed definition permits it." in recipe
    assert "GAP: in-flight evidence - query_readonly unavailable to this seat" in recipe
    assert "A correction supersedes its named earlier record." in recipe
    assert '"latest evidence", not "completion stage"' in recipe
    assert "author_type = 'task_attestation'" in recipe
    assert "task_claim_intervals" in recipe
    assert "limit=2000" in recipe


def test_in_flight_queries_are_single_ascii_read_only_statements(temp_db: HubDatabase) -> None:
    evidence_sql, closure_sql = _recipe_queries()
    window_start = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    for statement in (evidence_sql, closure_sql):
        assert statement.startswith(("SELECT", "WITH"))
        assert ";" not in statement
        assert statement.isascii()
        assert "t.project_id = %s" in statement
        assert "count(*) OVER ()" in statement
        assert "seq_num > %s" in statement

    # Each statement runs in a READ ONLY snapshot, so a data-modifying clause fails in
    # Postgres instead of passing on the text alone.
    empty_project = _project(temp_db)
    assert _read(temp_db, evidence_sql, (empty_project, ROOT_REGEX, 0)) == []
    assert _read(temp_db, closure_sql, (empty_project, window_start, ROOT_REGEX, 0)) == []


def test_in_flight_reports_latest_active_attestation(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    other_project_id = _project(temp_db)
    _seed_in_flight(temp_db, session_manager, sample_project["id"], other_project_id)
    evidence_sql, _ = _recipe_queries()

    rows = _read(temp_db, evidence_sql, (sample_project["id"], ROOT_REGEX, 0))

    assert rows
    assert all(set(row) == EVIDENCE_COLUMNS for row in rows)
    assert {row["total"] for row in rows} == {len(EXPECTED_EVIDENCE)}
    by_title = {row["title"]: row for row in rows}
    observed = {
        title: (row["evidence_kind"], row["evidence_result"]) for title, row in by_title.items()
    }
    assert observed == EXPECTED_EVIDENCE

    latest = by_title["latest wins"]
    assert latest["evidence_summary"] == "rerun green"
    # The hub hands JSON containers back as canonical text, so decode before comparing.
    assert json.loads(latest["evidence_reference"]) == {"scheme": "commit", "value": TIP}
    assert latest["evidence_recorded_at"] == LATEST_AT

    corrected = by_title["correction"]
    assert corrected["evidence_summary"] == "reviewer objected"
    assert corrected["evidence_reference"] is None

    claim_only = by_title["claim only"]
    assert claim_only["evidence_recorded_at"] is None
    assert claim_only["first_claimed_at"] is not None

    legacy = by_title["legacy steps only"]
    assert legacy["evidence_summary"] is None
    assert legacy["evidence_recorded_at"] is None

    attested = by_title["unclaimed attested"]
    assert attested["claimed_by_session_id"] is None
    assert attested["first_claimed_at"] is None


def test_in_flight_pages_keep_the_full_total_under_the_outer_limit(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    other_project_id = _project(temp_db)
    _seed_in_flight(temp_db, session_manager, project_id, other_project_id)
    evidence_sql, _ = _recipe_queries()

    first = _read(temp_db, evidence_sql, (project_id, ROOT_REGEX, 0), limit=2)
    assert first
    total = first[0]["total"]
    assert total == len(EXPECTED_EVIDENCE)

    collected = list(first)
    while len(collected) < total:
        page = _read(
            temp_db, evidence_sql, (project_id, ROOT_REGEX, collected[-1]["seq_num"]), limit=2
        )
        assert page, "keyset paging ended before the first page's total"
        collected.extend(page)

    seq_nums = [row["seq_num"] for row in collected]
    assert seq_nums == sorted(set(seq_nums))
    assert {row["title"] for row in collected} == set(EXPECTED_EVIDENCE)


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
