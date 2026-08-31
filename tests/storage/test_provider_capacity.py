from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.provider_capacity import ProviderCapacityStorage
from gobby.utils.machine_id import require_machine_id


@pytest.mark.integration
def test_provider_capacity_upsert_replaces_latest_machine_snapshot(
    postgres_db: HubDatabase,
) -> None:
    first_observed = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    second_observed = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    now = datetime(2026, 8, 30, 11, 1, tzinfo=UTC)
    storage = ProviderCapacityStorage(
        postgres_db,
        machine_id=require_machine_id(),
        clock=lambda: now,
    )

    storage.upsert(
        provider="agy",
        state="available",
        observed_at=first_observed,
        windows=(
            {
                "label": "Gemini Models — Weekly Limit Remaining",
                "used": 0.3,
                "limit": 1.0,
                "unit": "fraction",
                "resets_at": "2026-09-06T10:00:00Z",
            },
        ),
        reason=None,
        source_version="1.1.18",
    )
    storage.upsert(
        provider="agy",
        state="exhausted",
        observed_at=second_observed,
        windows=(
            {
                "label": "Gemini Models — Weekly Limit Remaining",
                "used": 1.0,
                "limit": 1.0,
                "unit": "fraction",
                "resets_at": "2026-09-06T10:00:00Z",
            },
        ),
        reason="weekly limit exhausted",
        source_version="1.1.19",
    )

    record = storage.get("agy")
    row = postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM provider_capacity_snapshots "
        "WHERE machine_id = %s AND provider = %s",
        (require_machine_id(), "agy"),
    )

    assert record is not None
    assert record.state == "exhausted"
    assert record.observed_at == second_observed
    assert record.windows[0]["used"] == 1.0
    assert record.reason == "weekly limit exhausted"
    assert record.source_version == "1.1.19"
    assert record.age_seconds == pytest.approx(60.0)
    assert row is not None
    assert row["count"] == 1
