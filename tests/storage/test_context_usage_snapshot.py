from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest

from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def usage_session_id(session_manager: SessionManager, sample_project: dict[str, str]) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    return session_manager.register(
        external_id="bigint-usage-session",
        machine_id=machine_id,
        source="claude",
        project_id=sample_project["id"],
    ).id


@pytest.mark.parametrize(
    "counter", ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"]
)
def test_update_usage_accepts_totals_above_int32(
    session_manager: SessionManager, usage_session_id: str, counter: str
) -> None:
    totals = dict.fromkeys(
        ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"], 0
    )
    totals[counter] = 2**31 + 123
    assert (
        session_manager.update_usage(
            usage_session_id,
            totals["input_tokens"],
            totals["output_tokens"],
            totals["cache_creation_tokens"],
            totals["cache_read_tokens"],
        )
        is True
    )
    session = session_manager.get(usage_session_id)
    assert session is not None
    assert getattr(session, f"usage_{counter}") == 2**31 + 123


@pytest.mark.parametrize(
    "counter", ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"]
)
def test_add_usage_delta_accumulates_past_int32(
    session_manager: SessionManager, usage_session_id: str, counter: str
) -> None:
    totals = dict.fromkeys(
        ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"], 0
    )
    totals[counter] = 2**31 - 1
    assert (
        session_manager.update_usage(
            usage_session_id,
            totals["input_tokens"],
            totals["output_tokens"],
            totals["cache_creation_tokens"],
            totals["cache_read_tokens"],
        )
        is True
    )
    deltas = dict.fromkeys(totals, 0)
    deltas[counter] = 1
    assert (
        session_manager.add_usage_delta(
            usage_session_id,
            deltas["input_tokens"],
            deltas["output_tokens"],
            deltas["cache_creation_tokens"],
            deltas["cache_read_tokens"],
        )
        is True
    )
    session = session_manager.get(usage_session_id)
    assert session is not None
    assert getattr(session, f"usage_{counter}") == 2**31


def test_usage_bigint_migration_preserves_values_defaults_and_constraint(
    session_manager: SessionManager, usage_session_id: str
) -> None:
    db = session_manager.db
    counters = (
        "usage_input_tokens",
        "usage_output_tokens",
        "usage_cache_creation_tokens",
        "usage_cache_read_tokens",
    )
    migration = (
        Path(__file__).resolve().parents[2]
        / "crates/gcore/assets/schema/migrations/459_session_usage_bigint.sql"
    ).read_text()
    with db.transaction():
        for column in counters:
            db.execute(f"ALTER TABLE sessions ALTER COLUMN {column} TYPE integer")
        db.execute(
            "UPDATE sessions SET usage_input_tokens = 2147483647, "
            "usage_output_tokens = 42, usage_cache_creation_tokens = NULL, "
            "usage_cache_read_tokens = 2147483647 WHERE id = %s",
            (usage_session_id,),
        )
        db.execute(migration)

    row = db.fetchone("SELECT * FROM sessions WHERE id = %s", (usage_session_id,))
    assert row is not None
    assert tuple(row[column] for column in counters) == (2**31 - 1, 42, None, 2**31 - 1)
    columns = db.fetchall(
        "SELECT column_name, data_type, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'sessions' "
        "AND column_name = ANY(%s) ORDER BY column_name",
        (list(counters),),
    )
    assert len(columns) == 4
    assert all(column["data_type"] == "bigint" for column in columns)
    assert all(column["column_default"] == "0" for column in columns)
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with db.transaction():
            db.execute(
                "UPDATE sessions SET usage_input_tokens = -1 WHERE id = %s", (usage_session_id,)
            )
    assert exc.value.diag.constraint_name == "sessions_context_usage_tokens_nonnegative"


def test_ratio_is_null_when_window_or_usage_is_unknown() -> None:
    assert ContextUsageSnapshot.calculate_ratio(None, 200_000) is None
    assert ContextUsageSnapshot.calculate_ratio(1_000, None) is None
    assert ContextUsageSnapshot.calculate_ratio(1_000, 0) is None


def test_ratio_is_clamped_to_valid_pressure_range() -> None:
    assert ContextUsageSnapshot.calculate_ratio(-10, 100) == 0.0
    assert ContextUsageSnapshot.calculate_ratio(50, 100) == 0.5
    assert ContextUsageSnapshot.calculate_ratio(150, 100) == 1.0


def test_token_breakdown_counts_cache_toward_prompt_footprint() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="codex",
        context_window=100_000,
        uncached_prompt_tokens=2_000,
        cache_read_tokens=30_000,
        cache_creation_tokens=3_000,
        output_tokens=500,
        model="gpt-5.3-codex",
    )

    assert snapshot.context_used_tokens == 35_000
    assert snapshot.raw_prompt_footprint == 35_000
    assert snapshot.uncached_prompt_tokens == 2_000
    assert snapshot.cache_read_tokens == 30_000
    assert snapshot.cache_creation_tokens == 3_000
    assert snapshot.output_tokens == 500
    assert snapshot.context_usage_ratio == 0.35
    assert snapshot.confidence == "reported"
    assert snapshot.source == "codex"
    assert snapshot.model == "gpt-5.3-codex"
    assert isinstance(snapshot.timestamp, datetime)
    assert snapshot.timestamp.tzinfo is UTC


def test_window_only_snapshot_keeps_pressure_unknown() -> None:
    snapshot = ContextUsageSnapshot.window_only(
        source="grok",
        context_window=512_000,
        model="grok-build",
    )

    assert snapshot.context_window == 512_000
    assert snapshot.context_used_tokens is None
    assert snapshot.context_usage_ratio is None
    assert snapshot.confidence == "unknown"


def test_reported_occupancy_stays_separate_from_token_breakdown() -> None:
    snapshot = ContextUsageSnapshot.from_reported_occupancy(
        source="codex",
        context_window=258_400,
        context_used_tokens=7_248,
        model="gpt-5.6-sol",
    )

    assert snapshot.context_used_tokens == 7_248
    assert snapshot.context_usage_ratio == pytest.approx(7_248 / 258_400)
    assert snapshot.raw_prompt_footprint == 7_248
    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens is None
    assert snapshot.output_tokens is None
    assert snapshot.confidence == "reported"


def test_token_breakdown_ignores_bool_token_values() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="web_chat",
        context_window=200_000,
        uncached_prompt_tokens=True,
        cache_read_tokens=False,
        cache_creation_tokens=True,
        output_tokens=False,
    )

    assert snapshot.context_used_tokens is None
    assert snapshot.raw_prompt_footprint is None
    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens is None
    assert snapshot.output_tokens is None
    assert snapshot.confidence == "unknown"


def test_token_breakdown_ignores_malformed_token_values() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="web_chat",
        context_window=200_000,
        uncached_prompt_tokens=cast(int, "not-a-number"),
        cache_read_tokens=cast(int, object()),
        cache_creation_tokens=cast(int, "5"),
        output_tokens=-3,
    )

    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens == 5
    assert snapshot.output_tokens == 0
