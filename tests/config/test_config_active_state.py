"""Published active epochs survive backend loss without exposing secret material."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime
from gobby.config.runtime_models import ApplyFailure, ConfigSnapshot, RuntimeSecretBinding
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from tests.config.test_config_runtime import (
    FakeNotificationSource,
    FakeRegistry,
    FakeRepository,
    FakeSubscriber,
    Prepared,
    stored_snapshot,
    wait_until,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_bundle_swap_publishes() -> None:
    repository = FakeRepository(
        [stored_snapshot(0), stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1})]
    )
    published: list[ConfigSnapshot] = []
    publisher_threads: list[int] = []

    def publish(snapshot: ConfigSnapshot) -> None:
        published.append(snapshot)
        publisher_threads.append(threading.get_ident())

    subscriber = FakeSubscriber("ui", {"ui.enabled"}, Prepared)
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber],
        active_state_publisher=publish,
    )
    try:
        started = await runtime.start()
        assert published == [started]
        assert runtime.ready
        reprepared = await runtime.reprepare_subscriber("ui")
        assert published[-1] is reprepared
        assert len(published) == 2
        registered = await runtime.register_subscriber(
            FakeSubscriber("other", {"test_mode"}, Prepared)
        )
        assert published[-1] is registered
        assert len(published) == 3
        repository.index = 1
        reconciled = await runtime.reconcile_revision(1)
        assert published[-1] is reconciled
        assert reconciled.active.ui.enabled is True
        assert len(published) == 4
        assert all(thread != threading.get_ident() for thread in publisher_threads)
    finally:
        await runtime.close()


@pytest.mark.integration
def test_document_seals_bindings(temp_db: HubDatabase, tmp_path: Path) -> None:
    from gobby.config.runtime_active_state import active_state_document

    store = SecretStore(temp_db, gobby_home=tmp_path)
    desired_binding = RuntimeSecretBinding("$secret:token", "desired-sensitive-token", "abc123")
    active_binding = RuntimeSecretBinding("$secret:token", None, "def456")
    snapshot = ConfigSnapshot(
        revision=7,
        desired=DaemonConfig(),
        active=DaemonConfig(),
        row_revisions={},
        pending_restart_keys=frozenset({"z", "a"}),
        failed_live_keys={
            "key": ApplyFailure(7, "subscriber", frozenset({"key"}), "private-error-text")
        },
        desired_values={"secret": "$secret:token", "sequence": (1, 2)},
        active_values={"secret": "$secret:token"},
        desired_overrides={"secret": "$secret:token"},
        desired_bindings={"secret": desired_binding},
        active_bindings={"secret": active_binding},
    )
    document = active_state_document(snapshot, secret_store=store, deployment_token="deployment")
    assert document["desired_values"] == {"secret": "$secret:token", "sequence": [1, 2]}
    assert document["active_values"] == {"secret": "$secret:token"}
    assert document["desired_overrides"] == {"secret": "$secret:token"}
    assert document["pending_restart_keys"] == ["a", "z"]
    assert document["failed_live_keys"] == {"key": {"revision": 7, "subscriber": "subscriber"}}
    desired = cast(dict[str, dict[str, object]], document["desired_bindings"])["secret"]
    assert desired["reference"] == "$secret:token"
    assert desired["fingerprint"] == "abc123"
    sealed = desired["sealed"]
    assert isinstance(sealed, str)
    aad = b"config-binding:deployment:secret:abc123"
    assert store.open_sealed(sealed, aad=aad) == b"desired-sensitive-token"
    active = cast(dict[str, dict[str, object]], document["active_bindings"])["secret"]
    assert active == {"reference": "$secret:token", "fingerprint": "def456", "sealed": None}
    serialized = json.dumps(document)
    assert "desired-sensitive-token" not in serialized
    assert "private-error-text" not in serialized
    assert snapshot.desired_bindings["secret"] == desired_binding
    assert snapshot.active_bindings["secret"] == active_binding


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_publish_retries_on_poll() -> None:
    class Repository(FakeRepository):
        def current_revision(self) -> int:
            return self.read().revision

    repository = Repository([stored_snapshot(0), stored_snapshot(1)])
    attempts: list[ConfigSnapshot] = []
    failed = True

    def publish(snapshot: ConfigSnapshot) -> None:
        attempts.append(snapshot)
        if failed:
            raise ConnectionError("hub unavailable")

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=FakeNotificationSource(),
        revision_poll_interval=0.01,
        active_state_publisher=publish,
    )
    try:
        await runtime.start()
        assert runtime.ready
        await wait_until(lambda: len(attempts) >= 2)
        repository.index = 1
        current = await runtime.reconcile_revision(1)
        failed = False
        await wait_until(lambda: not runtime._unpublished_active)
        assert attempts[-1] is current
        assert attempts[-1].revision == 1
        count = len(attempts)
        await runtime.close()
        assert len(attempts) == count
    finally:
        await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_live_epoch_is_published_before_activation_error() -> None:
    class Handle(Prepared):
        def activate(self) -> None:
            if self.value == 1:
                raise RuntimeError("activation failed")

    repository = FakeRepository(
        [stored_snapshot(0), stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1})]
    )
    published: list[ConfigSnapshot] = []
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[FakeSubscriber("ui", {"ui.enabled"}, Handle)],
        active_state_publisher=published.append,
    )
    try:
        await runtime.start()
        repository.index = 1
        with pytest.raises(RuntimeError, match="activation failed"):
            await runtime.reconcile_revision(1)
        assert len(published) == 2
        assert published[-1] is runtime.capture().snapshot
        assert published[-1].desired.ui.enabled is True
        assert published[-1].active.ui.enabled is False
        assert published[-1].failed_live_keys["ui.enabled"].subscriber == "ui"
    finally:
        await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publisher_cancellation_propagates_before_ready() -> None:
    def publish(snapshot: ConfigSnapshot) -> None:
        raise asyncio.CancelledError

    runtime = ConfigRuntime(
        FakeRepository([stored_snapshot(0)]),
        registry=FakeRegistry(),
        active_state_publisher=publish,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime.start()
        assert runtime.ready is False
    finally:
        await runtime.close()


@pytest.mark.integration
def test_publish_upserts_one_row_per_deployment(temp_db: HubDatabase, tmp_path: Path) -> None:
    from gobby.config.runtime_active_state import ActiveStatePublisher

    store = SecretStore(temp_db, gobby_home=tmp_path)
    publisher = ActiveStatePublisher(temp_db, store, "deployment-one")

    def snapshot(revision: int) -> ConfigSnapshot:
        return ConfigSnapshot(
            revision=revision,
            desired=DaemonConfig(),
            active=DaemonConfig(),
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={"test_mode": revision == 2},
            active_values={"test_mode": revision == 2},
        )

    def database_clock() -> datetime:
        with temp_db.transaction() as conn:
            row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None
        return cast(datetime, row["now"])

    publisher(snapshot(1))
    # The upsert must refresh published_at: bracket the second publish with the
    # database clock, so a timestamp kept from the first publish falls outside.
    before_second = database_clock()
    publisher(snapshot(2))
    after_second = database_clock()
    ActiveStatePublisher(temp_db, store, "deployment-two")(snapshot(3))
    with temp_db.transaction() as conn:
        conn.execute("SET LOCAL ROLE gobby_daemon_runtime")
        rows = conn.execute(
            "SELECT deployment_token, revision, state::text AS state, published_at "
            "FROM config_active_state ORDER BY deployment_token"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["deployment_token"] == "deployment-one"
    assert rows[0]["revision"] == 2
    state = json.loads(rows[0]["state"])
    assert state["active_values"] == {"test_mode": True}
    assert before_second <= rows[0]["published_at"] <= after_second
    assert rows[1]["deployment_token"] == "deployment-two"
    assert rows[1]["revision"] == 3
