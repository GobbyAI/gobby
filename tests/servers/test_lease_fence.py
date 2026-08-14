"""Lease fence admission, drain, and in-transaction epoch mutation tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.servers.lease_fence import (
    EffectFence,
    LeaseNotHeld,
    StaleEpochFence,
    bind_fenced_writer,
    drain_effect_fence,
    fenced_hub_write,
    run_hub_mutation,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _seed_runtime(db: HubDatabase, token: str, epoch: int) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS deployment_runtime (
            deployment_token TEXT PRIMARY KEY,
            fencing_epoch BIGINT NOT NULL DEFAULT 0,
            grant_signing_secret TEXT NOT NULL,
            epoch_updated_at TIMESTAMPTZ
        )
        """
    )
    db.execute(
        """
        INSERT INTO deployment_runtime (deployment_token, fencing_epoch, grant_signing_secret)
        VALUES (%s, %s, 'secret')
        ON CONFLICT (deployment_token) DO UPDATE
           SET fencing_epoch = EXCLUDED.fencing_epoch,
               grant_signing_secret = EXCLUDED.grant_signing_secret
        """,
        (token, epoch),
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lease_fence_probe (
            id TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def test_drain_stops_new_admissions_and_clears_in_flight() -> None:
    fence = EffectFence()
    with fence.admit():
        assert fence.in_flight == 1
        assert fence.serving is True
    drain_effect_fence(fence, timeout=0.2)
    assert fence.serving is False
    with pytest.raises(LeaseNotHeld):
        with fence.admit():
            pass


def test_drain_effect_fence_ignores_missing_fence() -> None:
    fence = EffectFence()
    drain_effect_fence(None)
    assert fence.serving is True
    with fence.admit():
        assert fence.in_flight == 1


def test_bound_writer_is_production_fenced_hub_write(temp_db: HubDatabase) -> None:
    token = "cafebabedeadbeef"
    _seed_runtime(temp_db, token, 1)
    lease = SimpleNamespace(deployment_token=token, fencing_epoch=1)
    bind_fenced_writer(temp_db, lease)

    def write_probe(value: str) -> None:
        def _write(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO lease_fence_probe (id, value) VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value
                """,
                ("probe", value),
            )

        run_hub_mutation(temp_db, _write)

    write_probe("owned")
    row = temp_db.fetchone("SELECT value FROM lease_fence_probe WHERE id = %s", ("probe",))
    assert row is not None
    assert row["value"] == "owned"

    temp_db.execute(
        "UPDATE deployment_runtime SET fencing_epoch = 2 WHERE deployment_token = %s",
        (token,),
    )
    with pytest.raises(StaleEpochFence):
        write_probe("stale")
    row = temp_db.fetchone("SELECT value FROM lease_fence_probe WHERE id = %s", ("probe",))
    assert row is not None
    assert row["value"] == "owned"


def test_fenced_hub_write_rejects_missing_runtime(temp_db: HubDatabase) -> None:
    _seed_runtime(temp_db, "other", 1)
    with pytest.raises(StaleEpochFence):
        fenced_hub_write(
            temp_db,
            deployment_token="missing",
            owned_epoch=1,
            writer=lambda _txn: None,
        )
