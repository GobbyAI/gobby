from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from time import perf_counter
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.unmodeled_observations import (
    UnmodeledObservationInput,
    UnmodeledObservationStore,
    stable_sample_hash,
)

pytestmark = pytest.mark.unit

# unmodeled_observation_events.session_id is a native uuid column.
SESSION_STORAGE = "aeaeaeae-0000-4000-8000-00000000ac01"


def test_large_event_table_batch_refresh_uses_occurrence_key_index(
    temp_db: HubDatabase,
) -> None:
    """A transcript batch must locate its rows rather than scan common tool history."""
    temp_db.execute(
        """
        INSERT INTO unmodeled_observation_events
            (id, session_id, source, kind, name, source_ref, sample_hash)
        SELECT md5(i::text)::uuid, %s::uuid, 'codex', 'block_type', 'scale_tool',
               i::text, 'seed'
        FROM generate_series(1, 300000) AS i
        """,
        (SESSION_STORAGE,),
    )
    temp_db.execute("ANALYZE unmodeled_observation_events")
    observations = [_observation("scale_tool", source_ref=str(i)) for i in range(120)]
    store = UnmodeledObservationStore(temp_db)
    real_transaction = temp_db.transaction
    statements: list[tuple[str, Any]] = []
    timings: list[float] = []

    @contextmanager
    def measured_transaction() -> Iterator[Transaction]:
        with real_transaction() as transaction:

            def execute(sql: str, parameters: Any = None) -> Any:
                started = perf_counter()
                try:
                    return transaction.execute(sql, parameters)
                finally:
                    statements.append((sql, parameters))
                    timings.append(perf_counter() - started)

            spy = MagicMock(wraps=transaction)
            spy.execute.side_effect = execute
            yield cast(Transaction, spy)

    with patch.object(temp_db, "transaction", side_effect=measured_transaction):
        assert store.record_many(observations) == 120
    refresh_sql, parameters = statements[1]
    row = temp_db.fetchone("EXPLAIN (FORMAT JSON) " + refresh_sql, parameters)
    assert row is not None
    plan = json.dumps(row["QUERY PLAN"])
    print(f"BATCH_PHASE_SECONDS {timings}")
    print(f"REFRESH_PLAN {plan}")
    assert "unmodeled_observation_events_dedup_key" in plan


def test_batch_records_content_free_statement_phases(
    temp_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.storage.unmodeled_observations")
    observation = _observation("private-tool-name")
    assert UnmodeledObservationStore(temp_db).record_many([observation]) == 1
    for phase in ("connection", "insert_events", "refresh_events", "refresh_aggregates", "commit"):
        assert f"phase={phase} event=started" in caplog.text
        assert f"phase={phase} event=finished" in caplog.text
    for private_value in (observation.name, SESSION_STORAGE, "secret-value", "payload"):
        assert private_value not in caplog.text


@pytest.mark.parametrize("session_id", [SESSION_STORAGE, None])
def test_batch_preserves_occurrence_counts_and_refreshes_timestamps(
    temp_db: HubDatabase, session_id: str | None
) -> None:
    store = UnmodeledObservationStore(temp_db)
    observations = [
        replace(_observation("batch_tool", source_ref=str(i)), session_id=session_id)
        for i in range(120)
    ]
    real_transaction = temp_db.transaction
    statement_counts: list[int] = []

    @contextmanager
    def counted_transaction() -> Iterator[Transaction]:
        with real_transaction() as transaction:
            spy = MagicMock(wraps=transaction)
            yield cast(Transaction, spy)
            statement_counts.append(spy.execute.call_count)

    with patch.object(temp_db, "transaction", side_effect=counted_transaction) as transactions:
        assert store.record_many(observations + observations[:20]) == 120
        assert transactions.call_count == 1
    assert statement_counts == [3]
    rows = store.list_observations(source="codex", kind="block_type")
    assert [(row.name, row.count) for row in rows] == [("batch_tool", 120)]
    temp_db.execute("UPDATE unmodeled_observations SET last_seen_at = NOW() - INTERVAL '1 day'")
    temp_db.execute(
        "UPDATE unmodeled_observation_events SET last_seen_at = NOW() - INTERVAL '1 day'"
    )
    previous = store.list_observations(source="codex", kind="block_type")[0].last_seen_at
    assert store.record_many(observations) == 0
    refreshed = store.list_observations(source="codex", kind="block_type")[0]
    assert refreshed.count == 120
    assert refreshed.last_seen_at > previous
    stale = temp_db.fetchone(
        "SELECT count(*) AS n FROM unmodeled_observation_events WHERE last_seen_at <= %s",
        (previous,),
    )
    assert stale is not None and stale["n"] == 0


def test_parallel_batches_count_distinct_occurrences_once(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    observations = [_observation("parallel_batch", source_ref=str(i)) for i in range(40)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.record_many, [observations, observations]))
    assert sum(results) == 40
    rows = store.list_observations(source="codex", kind="block_type")
    assert [(row.name, row.count) for row in rows] == [("parallel_batch", 40)]


def test_batch_keeps_the_last_novel_sample_when_replaying_old_occurrences(
    temp_db: HubDatabase,
) -> None:
    store = UnmodeledObservationStore(temp_db)
    old = replace(_observation("batch_sample", source_ref="1"), sample={"old": "value"})
    new = replace(_observation("batch_sample", source_ref="2"), sample={"new": "value"})
    assert store.record(old) is True
    assert store.record_many([new, old]) == 1
    row = store.list_observations(source="codex", kind="block_type")[0]
    assert row.count == 2
    assert row.sample_keys == ["new"]
    assert row.sample_hash == stable_sample_hash(new.sample)
    assert store.record_many([old]) == 0
    replayed = store.list_observations(source="codex", kind="block_type")[0]
    assert replayed.sample_keys == ["new"]
    assert replayed.sample_hash == row.sample_hash


def test_empty_or_unidentified_batch_does_not_open_a_transaction(
    temp_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    store = UnmodeledObservationStore(temp_db)
    with (
        patch.object(temp_db, "transaction", wraps=temp_db.transaction) as transactions,
        caplog.at_level("INFO", logger="gobby.storage.unmodeled_observations"),
    ):
        assert store.record_many([]) == 0
        assert store.record_many([replace(_observation("no_ref"), source_ref="")]) == 0
        assert transactions.call_count == 0
    assert "Unmodeled transcript block observed without stable source_ref" in caplog.text


def test_batch_normalizes_uuid_case_when_selecting_the_novel_sample(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    old = replace(_observation("uuid_case", source_ref="1"), sample={"old": "value"})
    new = replace(
        _observation("uuid_case", source_ref="2"),
        session_id=SESSION_STORAGE.upper(),
        sample={"new": "value"},
    )
    assert store.record(old) is True
    assert store.record_many([old, new]) == 1
    row = store.list_observations(source="codex", kind="block_type")[0]
    assert row.count == 2
    assert row.sample_keys == ["new"]
    assert row.sample_hash == stable_sample_hash(new.sample)


def test_prune_between_duplicate_insert_and_refresh_leaves_no_zero_count_aggregate(
    temp_db: HubDatabase,
) -> None:
    store = UnmodeledObservationStore(temp_db)
    observation = _observation("retention_race")
    assert store.record(observation) is True
    temp_db.execute(
        "UPDATE unmodeled_observation_events SET last_seen_at = NOW() - INTERVAL '2 days'"
    )
    real_transaction = temp_db.transaction
    pruned: list[int] = []
    with ThreadPoolExecutor(max_workers=1) as executor:

        @contextmanager
        def interleaved_transaction() -> Iterator[Transaction]:
            with real_transaction() as transaction:
                spy = MagicMock(wraps=transaction)

                def execute(sql: str, parameters: Any = None) -> Any:
                    result = transaction.execute(sql, parameters)
                    if sql.lstrip().startswith("INSERT INTO unmodeled_observation_events"):
                        pruned.append(
                            executor.submit(store.prune_events_older_than, retention_days=1).result(
                                timeout=5
                            )
                        )
                    return result

                spy.execute.side_effect = execute
                yield cast(Transaction, spy)

        with patch.object(temp_db, "transaction", side_effect=interleaved_transaction):
            assert store.record_many([observation]) == 0
    assert pruned == [1]
    assert store.list_observations(source="codex", kind="block_type") == []


def test_parallel_mixed_replays_and_novel_keys_keep_both_counts(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    first = _observation("mixed_a", source_ref="1")
    second = _observation("mixed_b", source_ref="1")
    assert store.record_many([first, second]) == 2
    batches = [
        [replace(first, source_ref="2"), second],
        [first, replace(second, source_ref="2")],
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(store.record_many, batches)) == [1, 1]
    rows = store.list_observations(source="codex", kind="block_type")
    assert sorted((row.name, row.count) for row in rows) == [("mixed_a", 2), ("mixed_b", 2)]


def _observation(name: str, *, source_ref: str = "42") -> UnmodeledObservationInput:
    return UnmodeledObservationInput(
        session_id=SESSION_STORAGE,
        source="codex",
        kind="block_type",
        name=name,
        source_ref=source_ref,
        source_line=int(source_ref),
        sample={"type": name, "token": "secret-value", "payload": {"value": "kept"}},
    )


def test_stable_sample_hash_uses_structure_not_scalar_values() -> None:
    assert stable_sample_hash({"token": "secret-a", "payload": {"value": "one"}}) == (
        stable_sample_hash({"token": "secret-b", "payload": {"value": "two"}})
    )


def test_novel_occurrence_inserts_event_and_aggregate(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    observation = _observation("storage_novel")

    assert store.record(observation) is True

    events = temp_db.fetchall(
        "SELECT source_ref, sample_hash FROM unmodeled_observation_events WHERE name = %s",
        (observation.name,),
    )
    rows = store.list_observations(source="codex", kind="block_type")

    matching = [row for row in rows if row.name == observation.name]
    assert len(events) == 1
    assert events[0]["source_ref"] == "42"
    assert len(matching) == 1
    assert matching[0].count == 1
    assert matching[0].sample_keys == ["payload", "token", "type"]
    assert matching[0].sample_hash == stable_sample_hash(observation.sample)


def test_duplicate_reprocess_keeps_count_one_and_moves_last_seen(
    temp_db: HubDatabase,
) -> None:
    store = UnmodeledObservationStore(temp_db)
    observation = _observation("storage_duplicate", source_ref="43")

    assert store.record(observation) is True
    temp_db.execute(
        """
        UPDATE unmodeled_observations
        SET last_seen_at = NOW() - INTERVAL '1 day'
        WHERE source = %s AND kind = %s AND name = %s AND server_name = %s AND tool_type = %s
        """,
        ("codex", "block_type", observation.name, "", ""),
    )
    before_rows = [
        row
        for row in store.list_observations(source="codex", kind="block_type")
        if row.name == observation.name
    ]
    before = before_rows[0].last_seen_at
    before_event_row = temp_db.fetchone(
        "SELECT last_seen_at FROM unmodeled_observation_events WHERE name = %s",
        (observation.name,),
    )
    assert before_event_row is not None
    before_event = before_event_row["last_seen_at"]

    assert store.record(observation) is False

    rows = [
        row
        for row in store.list_observations(source="codex", kind="block_type")
        if row.name == observation.name
    ]
    event_rows = temp_db.fetchall(
        "SELECT id, last_seen_at FROM unmodeled_observation_events WHERE name = %s",
        (observation.name,),
    )
    assert len(event_rows) == 1
    assert rows[0].count == 1
    assert rows[0].last_seen_at != before
    assert event_rows[0]["last_seen_at"] != before_event


def test_parallel_writers_of_same_occurrence_count_once(temp_db: HubDatabase) -> None:
    observation = _observation("storage_parallel", source_ref="44")

    def write_once() -> bool:
        return UnmodeledObservationStore(temp_db).record(observation)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _idx: write_once(), range(8)))

    rows = [
        row
        for row in UnmodeledObservationStore(temp_db).list_observations(
            source="codex",
            kind="block_type",
        )
        if row.name == observation.name
    ]
    events = temp_db.fetchall(
        "SELECT id FROM unmodeled_observation_events WHERE name = %s",
        (observation.name,),
    )
    assert results.count(True) == 1
    assert len(events) == 1
    assert rows[0].count == 1


def test_prune_events_recomputes_retention_window_aggregates(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    stale_only_observation = _observation("storage_old", source_ref="45")
    old_mixed_observation = _observation("storage_mixed", source_ref="46")
    fresh_mixed_observation = _observation("storage_mixed", source_ref="47")
    store.record(stale_only_observation)
    store.record(old_mixed_observation)
    store.record(fresh_mixed_observation)
    temp_db.execute(
        "UPDATE unmodeled_observation_events SET last_seen_at = NOW() - INTERVAL '2 days' "
        "WHERE source_ref IN (%s, %s)",
        (stale_only_observation.source_ref, old_mixed_observation.source_ref),
    )

    assert store.prune_events_older_than(retention_days=1) == 2

    remaining = temp_db.fetchall(
        "SELECT source_ref FROM unmodeled_observation_events WHERE name IN (%s, %s)",
        (stale_only_observation.name, fresh_mixed_observation.name),
    )
    assert {row["source_ref"] for row in remaining} == {fresh_mixed_observation.source_ref}

    rows = store.list_observations(source="codex", kind="block_type")
    counts_by_name = {row.name: row.count for row in rows}
    assert stale_only_observation.name not in counts_by_name
    assert counts_by_name[fresh_mixed_observation.name] == 1
