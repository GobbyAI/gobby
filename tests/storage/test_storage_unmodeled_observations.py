from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.unmodeled_observations import (
    UnmodeledObservationInput,
    UnmodeledObservationStore,
    stable_sample_hash,
)

pytestmark = pytest.mark.unit

# unmodeled_observation_events.session_id is a native uuid column.
SESSION_STORAGE = "aeaeaeae-0000-4000-8000-00000000ac01"


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
