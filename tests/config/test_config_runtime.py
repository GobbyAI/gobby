from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import cast

import psycopg
import pytest

from gobby.config.app import DaemonConfig
from gobby.config.feature_base import candidate_labels
from gobby.config.registry import ActivationPolicy
from gobby.config.runtime import (
    ConfigChange,
    ConfigRuntime,
    SecretIdentityMismatchError,
    UnavailableService,
)
from gobby.config.runtime_models import ConfigSnapshot
from gobby.storage.config_notifications import ConfigNotificationListener, NotificationConnection
from gobby.storage.hub.postgres import PostgresHubDatabase

FEATURE_LOW_PROFILE_DEFAULT_KEY = "ai.generation.profile_defaults.feature_low"


@dataclass(frozen=True, slots=True)
class StoredBinding:
    reference: str
    plaintext: str | None


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    revision: int
    values: MappingProxyType[str, object]
    overrides: MappingProxyType[str, object]
    row_revisions: MappingProxyType[str, int]
    secret_bindings: MappingProxyType[str, StoredBinding]


class FakeRepository:
    def __init__(self, snapshots: list[StoredSnapshot]) -> None:
        self.snapshots = snapshots
        self.index = 0
        self.read_count = 0
        self.bounds: list[tuple[int, int]] = []
        self.candidate_inputs: list[dict[str, object]] = []

    def read(self, *, resolve_secrets: bool = True) -> StoredSnapshot:
        assert resolve_secrets
        self.read_count += 1
        return self.snapshots[self.index]

    def read_bounded(
        self,
        *,
        resolve_secrets: bool = True,
        statement_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> StoredSnapshot:
        self.bounds.append((statement_timeout_ms, lock_timeout_ms))
        return self.read(resolve_secrets=resolve_secrets)

    def runtime_candidate(
        self, overrides: dict[str, object], _secret_bindings: object
    ) -> DaemonConfig:
        self.candidate_inputs.append(dict(overrides))
        candidate: dict[str, object] = {}
        if "ui.enabled" in overrides:
            candidate["ui"] = {"enabled": overrides["ui.enabled"]}
        if "test_mode" in overrides:
            candidate["test_mode"] = overrides["test_mode"]
        if FEATURE_LOW_PROFILE_DEFAULT_KEY in overrides:
            candidate["ai"] = {
                "generation": {
                    "profile_defaults": {"feature_low": overrides[FEATURE_LOW_PROFILE_DEFAULT_KEY]}
                }
            }
        if "session_summary.candidates" in overrides:
            candidate["session_summary"] = {"candidates": overrides["session_summary.candidates"]}
        return DaemonConfig.model_validate(candidate)


@dataclass(frozen=True, slots=True)
class FakeSpec:
    activation: ActivationPolicy


class FakeRegistry:
    def __init__(self, restart_keys: set[str] | None = None) -> None:
        self.restart_keys = restart_keys or {"test_mode"}

    def resolve(self, key: str) -> FakeSpec:
        activation = (
            ActivationPolicy.RESTART_REQUIRED if key in self.restart_keys else ActivationPolicy.LIVE
        )
        return FakeSpec(activation)


class Prepared:
    def __init__(self, value: object) -> None:
        self.value = value
        self.dispose_count = 0
        self.disposed = threading.Event()

    def dispose(self) -> None:
        self.dispose_count += 1
        self.disposed.set()


class FakeSubscriber:
    def __init__(
        self,
        name: str,
        keys: set[str],
        prepare: Callable[[int], Prepared],
        *,
        required: bool = True,
        prepare_timeout: float = 0.2,
        dispose_timeout: float = 0.2,
    ) -> None:
        self.name = name
        self.keys = frozenset(keys)
        self.required = required
        self.prepare_timeout = prepare_timeout
        self.dispose_timeout = dispose_timeout
        self._prepare = prepare
        self.revisions: list[int] = []
        self.thread_ids: list[int] = []

    def prepare(self, change: ConfigChange) -> Prepared:
        revision = change.revision
        self.revisions.append(revision)
        self.thread_ids.append(threading.get_ident())
        return self._prepare(revision)


class FakeNotificationSource:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[int | BaseException] = asyncio.Queue()
        self.connect_count = 0
        self.close_count = 0

    async def connect(self) -> None:
        self.connect_count += 1

    async def revisions(self) -> AsyncIterator[int]:
        while True:
            item = await self.queue.get()
            if isinstance(item, BaseException):
                self.queue.task_done()
                raise item
            try:
                yield item
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        self.close_count += 1

    async def emit(self, revision: int) -> None:
        await self.queue.put(revision)

    async def fail(self) -> None:
        await self.queue.put(ConnectionError("database restarted"))


def stored_snapshot(
    revision: int,
    *,
    ui_enabled: bool = False,
    test_mode: bool = False,
    changed: Mapping[str, int] | None = None,
    secrets: Mapping[str, StoredBinding] | None = None,
    values: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> StoredSnapshot:
    complete_values: dict[str, object] = {"ui.enabled": ui_enabled, "test_mode": test_mode}
    complete_values.update(values or {})
    stored_values = MappingProxyType(complete_values)
    stored_overrides = MappingProxyType(
        dict(complete_values) if overrides is None else dict(overrides)
    )
    return StoredSnapshot(
        revision=revision,
        values=stored_values,
        overrides=stored_overrides,
        row_revisions=MappingProxyType(dict(changed or {})),
        secret_bindings=MappingProxyType(dict(secrets or {})),
    )


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_snapshot_swap_is_atomic() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0, ui_enabled=False),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()

    old_bundle = runtime.capture()
    repository.index = 1
    await runtime.reconcile_revision(1)
    new_bundle = runtime.capture()

    assert old_bundle.snapshot.revision == 0
    assert old_bundle.snapshot.active.ui.enabled is False
    assert old_bundle.snapshot.desired.ui.enabled is False
    assert new_bundle.snapshot.revision == 1
    assert new_bundle.snapshot.active.ui.enabled is True
    assert new_bundle.snapshot.desired.ui.enabled is True
    assert old_bundle is not new_bundle
    with pytest.raises(TypeError):
        cast(dict[str, int], new_bundle.snapshot.row_revisions)["ui.enabled"] = 99

    await runtime.close()


@pytest.mark.unit
def test_snapshot_overrides_are_deeply_isolated_at_publish_and_access() -> None:
    config = DaemonConfig()
    overrides: dict[str, object] = {"provider": {"models": ["original"]}}
    snapshot = ConfigSnapshot(
        revision=1,
        desired=config,
        active=config,
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_overrides=overrides,
        active_overrides=overrides,
    )

    cast(dict[str, list[str]], overrides["provider"])["models"].append("caller-mutation")
    desired = cast(dict[str, list[str]], snapshot.desired_overrides["provider"])
    active = cast(dict[str, list[str]], snapshot.active_overrides["provider"])
    desired["models"].append("reader-mutation")
    active["models"].append("reader-mutation")

    assert snapshot.desired_overrides == {"provider": {"models": ["original"]}}
    assert snapshot.active_overrides == {"provider": {"models": ["original"]}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sparse_profile_override_propagates_to_omitted_feature_candidates() -> None:
    low_candidates = ["codex/gpt-5.6-luna", "claude/haiku"]
    repository = FakeRepository(
        [
            stored_snapshot(
                1,
                values={
                    FEATURE_LOW_PROFILE_DEFAULT_KEY: low_candidates,
                    "session_summary.candidates": ["claude/haiku"],
                },
                overrides={FEATURE_LOW_PROFILE_DEFAULT_KEY: low_candidates},
            )
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())

    await runtime.start()

    assert candidate_labels(runtime.snapshot.active.session_summary.candidates) == tuple(
        low_candidates
    )
    assert repository.candidate_inputs[-1] == {FEATURE_LOW_PROFILE_DEFAULT_KEY: low_candidates}
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_profile_override_updates_inherited_feature_candidates() -> None:
    initial = ["codex/gpt-5.6-luna", "claude/haiku"]
    updated = ["endpoint:local/qwen", "claude/haiku"]
    repository = FakeRepository(
        [
            stored_snapshot(
                1,
                values={FEATURE_LOW_PROFILE_DEFAULT_KEY: initial},
                overrides={FEATURE_LOW_PROFILE_DEFAULT_KEY: initial},
                changed={FEATURE_LOW_PROFILE_DEFAULT_KEY: 1},
            ),
            stored_snapshot(
                2,
                values={FEATURE_LOW_PROFILE_DEFAULT_KEY: updated},
                overrides={FEATURE_LOW_PROFILE_DEFAULT_KEY: updated},
                changed={FEATURE_LOW_PROFILE_DEFAULT_KEY: 2},
            ),
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()
    repository.index = 1

    await runtime.reconcile_revision(2)

    assert candidate_labels(runtime.snapshot.active.session_summary.candidates) == tuple(updated)
    assert runtime.snapshot.active_overrides == {FEATURE_LOW_PROFILE_DEFAULT_KEY: updated}
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_feature_candidates_override_profile_default() -> None:
    profile_candidates = ["codex/gpt-5.6-luna", "claude/haiku"]
    explicit_candidates = ["endpoint:local/qwen"]
    repository = FakeRepository(
        [
            stored_snapshot(
                1,
                values={
                    FEATURE_LOW_PROFILE_DEFAULT_KEY: profile_candidates,
                    "session_summary.candidates": explicit_candidates,
                },
                overrides={
                    FEATURE_LOW_PROFILE_DEFAULT_KEY: profile_candidates,
                    "session_summary.candidates": explicit_candidates,
                },
            )
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())

    await runtime.start()

    assert candidate_labels(runtime.snapshot.active.session_summary.candidates) == tuple(
        explicit_candidates
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_restart_policy_tracks_pending_keys() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, test_mode=True, changed={"test_mode": 1}),
            stored_snapshot(2, test_mode=False, changed={"test_mode": 2}),
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()

    repository.index = 1
    changed = await runtime.reconcile_revision(1)
    assert changed.desired.test_mode is True
    assert changed.active.test_mode is False
    assert changed.pending_restart_keys == frozenset({"test_mode"})

    repository.index = 2
    reverted = await runtime.reconcile_revision(2)
    assert reverted.desired.test_mode is False
    assert reverted.active.test_mode is False
    assert reverted.pending_restart_keys == frozenset()
    await runtime.close()


@pytest.mark.asyncio
async def test_apply_failure_preserves_local_last_good_state() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )

    def fail(revision: int) -> Prepared:
        if revision:
            raise RuntimeError("constructor failed")
        return Prepared("initial")

    subscriber = FakeSubscriber("ui", {"ui.enabled"}, fail)
    runtime = ConfigRuntime(repository, registry=FakeRegistry(), subscribers=[subscriber])
    await runtime.start()
    repository.index = 1

    snapshot = await runtime.reconcile_local_commit(1)

    assert snapshot.revision == 1
    assert snapshot.desired.ui.enabled is True
    assert snapshot.active.ui.enabled is False
    assert snapshot.failed_live_keys["ui.enabled"].subscriber == "ui"
    assert repository.read_count == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_remote_runtime_receives_revision_notification() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    notifications = FakeNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
    )
    await runtime.start()
    repository.index = 1

    await notifications.emit(1)
    await wait_until(lambda: runtime.snapshot.revision == 1)

    assert runtime.snapshot.active.ui.enabled is True
    await runtime.close()


@pytest.mark.asyncio
async def test_listener_reconnect_reloads_snapshot() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    notifications = FakeNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
        reconnect_backoff=0.001,
    )
    await runtime.start()
    repository.index = 1

    await notifications.fail()
    await wait_until(lambda: notifications.connect_count == 2)
    await wait_until(lambda: runtime.snapshot.revision == 1 and runtime.healthy)

    assert notifications.close_count >= 1
    await runtime.close()


@pytest.mark.asyncio
async def test_notifications_are_idempotent_and_coalesced() -> None:
    repository = FakeRepository(
        [stored_snapshot(0), stored_snapshot(3, ui_enabled=True, changed={"ui.enabled": 3})]
    )
    notifications = FakeNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
    )
    await runtime.start()
    repository.index = 1

    for revision in (0, 1, 1, 2, 3, 3):
        await notifications.emit(revision)
    await wait_until(lambda: runtime.snapshot.revision == 3)
    await notifications.queue.join()

    assert repository.read_count == 2
    await runtime.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_listener_assumes_runtime_role() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if dsn is None:
        pytest.skip("DATABASE_URL is required for PostgreSQL listener validation")
    database = PostgresHubDatabase(dsn, runtime_role="gobby_daemon_runtime")
    opened: list[psycopg.AsyncConnection[dict[str, object]]] = []

    async def factory() -> NotificationConnection:
        connection = cast(
            psycopg.AsyncConnection[dict[str, object]],
            await database.open_runtime_async_connection(),
        )
        opened.append(connection)
        return connection

    listener = ConfigNotificationListener(factory)
    await listener.connect()
    cursor = await opened[0].execute("SELECT current_user")
    row = await cursor.fetchone()
    sender = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        revision, _cursor = await asyncio.wait_for(
            asyncio.gather(
                anext(listener.revisions()),
                sender.execute("SELECT pg_notify('gobby_config_changed', '7')"),
            ),
            1,
        )
    finally:
        await sender.close()
    await listener.close()
    database.close()

    assert row is not None
    assert row["current_user"] == "gobby_daemon_runtime"
    assert opened[0].autocommit is True
    assert revision == 7
    assert opened[0].closed is True


class DelayedRepository(FakeRepository):
    def __init__(self, snapshots: list[StoredSnapshot]) -> None:
        super().__init__(snapshots)
        self.block_next = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def read_bounded(
        self,
        *,
        resolve_secrets: bool = True,
        statement_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> StoredSnapshot:
        if self.block_next:
            self.block_next = False
            captured = self.snapshots[self.index]
            self.entered.set()
            self.release.wait(1)
            self.read_count += 1
            return captured
        return super().read_bounded(
            resolve_secrets=resolve_secrets,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )


@pytest.mark.asyncio
async def test_out_of_order_reload_is_discarded() -> None:
    repository = DelayedRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
            stored_snapshot(2, ui_enabled=False, changed={"ui.enabled": 2}),
        ]
    )
    prepared_revisions: list[int] = []

    def prepare(revision: int) -> Prepared:
        prepared_revisions.append(revision)
        return Prepared(revision)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[FakeSubscriber("ui", {"ui.enabled"}, prepare)],
    )
    await runtime.start()
    repository.index = 1
    repository.block_next = True
    older = asyncio.create_task(runtime.reconcile_revision(1))
    await asyncio.to_thread(repository.entered.wait, 1)
    repository.index = 2
    newer = asyncio.create_task(runtime.reconcile_revision(2))
    repository.release.set()

    await asyncio.gather(older, newer)

    assert runtime.snapshot.revision == 2
    assert 1 not in prepared_revisions
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_apply_preserves_active_secret_binding() -> None:
    old = StoredBinding("secret://shared", "old-payload")
    rotated = StoredBinding("secret://shared", "new-payload")
    repository = FakeRepository(
        [
            stored_snapshot(0, secrets={"api.key": old}),
            stored_snapshot(1, changed={"api.key": 1}, secrets={"api.key": rotated}),
        ]
    )

    def fail(revision: int) -> Prepared:
        if revision:
            raise RuntimeError("client rejected credentials")
        return Prepared("initial")

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[FakeSubscriber("client", {"api.key"}, fail)],
    )
    await runtime.start()
    repository.index = 1

    snapshot = await runtime.reconcile_revision(1)

    assert snapshot.desired_secret("api.key") == "new-payload"
    assert snapshot.active_secret("api.key") == "old-payload"
    assert snapshot.desired_secret_fingerprint("api.key") != snapshot.active_secret_fingerprint(
        "api.key"
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_blocking_work_is_bounded_and_quarantined() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    release = threading.Event()
    late = Prepared("late")

    def blocked(revision: int) -> Prepared:
        if revision:
            release.wait(1)
            return late
        return Prepared("initial")

    subscriber = FakeSubscriber(
        "ui",
        {"ui.enabled"},
        blocked,
        prepare_timeout=0.02,
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry(), subscribers=[subscriber])
    await runtime.start()
    repository.index = 1
    main_thread = threading.get_ident()

    before = time.monotonic()
    snapshot = await runtime.reconcile_revision(1)
    elapsed = time.monotonic() - before
    release.set()
    await asyncio.to_thread(late.disposed.wait, 1)

    assert elapsed < 0.2
    assert snapshot.active.ui.enabled is False
    assert repository.bounds == [(5_000, 5_000), (5_000, 5_000)]
    assert subscriber.thread_ids[-1] != main_thread
    assert late.dispose_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_superseded_preparation_is_disposed() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
            stored_snapshot(2, ui_enabled=False, changed={"ui.enabled": 2}),
        ]
    )
    entered = threading.Event()
    release = threading.Event()
    obsolete = Prepared("revision-1")

    def prepare(revision: int) -> Prepared:
        if revision == 1:
            entered.set()
            release.wait(1)
            return obsolete
        return Prepared(f"revision-{revision}")

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[FakeSubscriber("ui", {"ui.enabled"}, prepare)],
    )
    await runtime.start()
    repository.index = 1
    first = asyncio.create_task(runtime.reconcile_revision(1))
    await asyncio.to_thread(entered.wait, 1)
    repository.index = 2
    second = asyncio.create_task(runtime.reconcile_revision(2))
    release.set()

    await asyncio.gather(first, second)

    assert runtime.snapshot.revision == 2
    assert obsolete.dispose_count == 1
    assert runtime.snapshot.failed_live_keys == {}
    await runtime.close()


@pytest.mark.asyncio
async def test_active_bundle_swap_is_atomic() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    initial = Prepared("service-0")
    replacement = Prepared("service-1")
    subscriber = FakeSubscriber(
        "ui",
        {"ui.enabled"},
        lambda revision: initial if revision == 0 else replacement,
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry(), subscribers=[subscriber])
    await runtime.start()
    old = runtime.capture()
    repository.index = 1

    await runtime.reconcile_revision(1)
    new = runtime.capture()

    assert (old.snapshot.revision, old.services["ui"]) == (0, "service-0")
    assert (new.snapshot.revision, new.services["ui"]) == (1, "service-1")
    assert initial.dispose_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_kek_mismatch_fails_closed() -> None:
    repository = FakeRepository([stored_snapshot(0)])
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        expected_secret_identity="shared-kek-dek",
        secret_identity_verifier=lambda: "different-kek-dek",
    )

    with pytest.raises(SecretIdentityMismatchError):
        await runtime.start()

    assert runtime.ready is False
    assert runtime.healthy is False
    with pytest.raises(RuntimeError, match="has not started"):
        runtime.capture()
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_live_record_lifecycle() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
            stored_snapshot(
                2,
                ui_enabled=True,
                test_mode=True,
                changed={"ui.enabled": 1, "test_mode": 2},
            ),
            stored_snapshot(3, ui_enabled=False, changed={"ui.enabled": 3, "test_mode": 2}),
        ]
    )
    fail = True

    def prepare(revision: int) -> Prepared:
        if fail and revision:
            raise RuntimeError(f"failed revision {revision}")
        return Prepared(revision)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[FakeSubscriber("ui", {"ui.enabled"}, prepare)],
    )
    await runtime.start()
    repository.index = 1
    await runtime.reconcile_revision(1)
    assert "ui.enabled" in runtime.snapshot.failed_live_keys

    repository.index = 2
    await runtime.reconcile_revision(2)
    await runtime.reconcile_revision(2)
    assert "ui.enabled" in runtime.snapshot.failed_live_keys

    fail = False
    repository.index = 3
    await runtime.reconcile_revision(3)
    assert "ui.enabled" not in runtime.snapshot.failed_live_keys
    await runtime.close()


@pytest.mark.asyncio
async def test_successful_rebuild_drops_failures_recorded_under_other_keys() -> None:
    """A successful subscriber replacement supersedes its earlier failures even
    when the originally failing key is not part of the new change set."""
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
            stored_snapshot(
                2,
                ui_enabled=True,
                test_mode=True,
                changed={"ui.enabled": 1, "test_mode": 2},
            ),
        ]
    )
    fail = True

    def prepare(revision: int) -> Prepared:
        if fail and revision:
            raise RuntimeError(f"failed revision {revision}")
        return Prepared(revision)

    runtime = ConfigRuntime(
        repository,
        # Keep every stored key LIVE (an empty restart set falls back to the
        # fake's default, which would restart-gate test_mode).
        registry=FakeRegistry(restart_keys={"unused.key"}),
        subscribers=[FakeSubscriber("ui", {"ui.enabled", "test_mode"}, prepare)],
    )
    await runtime.start()
    repository.index = 1
    await runtime.reconcile_revision(1)
    assert "ui.enabled" in runtime.snapshot.failed_live_keys

    fail = False
    repository.index = 2
    await runtime.reconcile_revision(2)
    assert runtime.snapshot.failed_live_keys == {}
    assert runtime.snapshot.active.ui.enabled is True
    await runtime.close()


@pytest.mark.asyncio
async def test_lane_saturation_preserves_bounds() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
            stored_snapshot(2, ui_enabled=False, changed={"ui.enabled": 2}),
        ]
    )
    never = threading.Event()

    def blocked(revision: int) -> Prepared:
        if revision:
            never.wait(5)
            return Prepared("abandoned")
        return Prepared("initial")

    subscriber = FakeSubscriber(
        "ui",
        {"ui.enabled"},
        blocked,
        prepare_timeout=0.02,
    )
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber],
        constructor_workers=1,
        constructor_admission_timeout=0.02,
    )
    await runtime.start()
    repository.index = 1
    await asyncio.wait_for(runtime.reconcile_local_commit(1), 0.2)
    repository.index = 2
    await asyncio.wait_for(runtime.reconcile_local_commit(2), 0.2)

    before = time.monotonic()
    await runtime.close()
    assert time.monotonic() - before < 0.2
    never.set()


@pytest.mark.asyncio
async def test_first_initialization_failure_semantics() -> None:
    repository = FakeRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    partial = Prepared("partial")
    required_runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[
            FakeSubscriber("first", {"ui.enabled"}, lambda _revision: partial),
            FakeSubscriber(
                "required",
                {"ui.enabled"},
                lambda _revision: (_ for _ in ()).throw(RuntimeError("required failed")),
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="required failed"):
        await required_runtime.start()
    assert partial.dispose_count == 1
    assert required_runtime.ready is False
    await required_runtime.close()

    should_fail = True

    def optional_prepare(revision: int) -> Prepared:
        if should_fail:
            raise RuntimeError("optional unavailable")
        return Prepared(revision)

    optional = FakeSubscriber(
        "optional",
        {"ui.enabled"},
        optional_prepare,
        required=False,
    )
    optional_runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[optional],
    )
    initial = await optional_runtime.start()
    assert optional_runtime.ready is True
    assert optional_runtime.degraded is True
    assert isinstance(optional_runtime.capture().services["optional"], UnavailableService)
    assert "ui.enabled" in initial.failed_live_keys

    should_fail = False
    repository.index = 1
    recovered = await optional_runtime.reconcile_revision(1)
    assert optional_runtime.degraded is False
    assert "ui.enabled" not in recovered.failed_live_keys
    assert optional_runtime.capture().services["optional"] == 1
    await optional_runtime.close()


@pytest.mark.asyncio
async def test_bogus_revision_request_unwedges_and_adopts_stored() -> None:
    repository = FakeRepository([stored_snapshot(0)])
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()

    snapshot = await runtime.reconcile_revision(1 << 60)

    assert snapshot.revision == 0
    assert runtime._max_requested_revision == 0
    assert repository.read_count <= 6

    repository.snapshots.append(stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}))
    repository.index = 1
    reconciled = await runtime.reconcile_revision(1)
    assert reconciled.revision == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_listener_drops_out_of_domain_notification_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.storage.config_notifications")

    class _Conn:
        autocommit = True

        async def execute(self, command: str) -> object:
            return None

        def notifies(
            self,
            *,
            timeout: float | None = None,
            stop_after: int | None = None,
        ) -> AsyncIterator[SimpleNamespace]:
            async def _gen() -> AsyncIterator[SimpleNamespace]:
                for payload in ("garbage", str(1 << 60), "7"):
                    yield SimpleNamespace(payload=payload)

            return _gen()

        async def close(self) -> None:
            return None

    async def _factory() -> NotificationConnection:
        return cast(NotificationConnection, _Conn())

    listener = ConfigNotificationListener(_factory)
    await listener.connect()
    revisions = [revision async for revision in listener.revisions()]
    await listener.close()

    assert revisions == [7]
    assert "garbage" in caplog.text
    assert str(1 << 60) in caplog.text


@pytest.mark.asyncio
async def test_concurrent_revision_request_survives_bogus_request_adoption() -> None:
    class BlockingFinalReadRepository(FakeRepository):
        def __init__(self, snapshots: list[StoredSnapshot]) -> None:
            super().__init__(snapshots)
            self.final_read_entered = threading.Event()
            self.release_final_read = threading.Event()

        def read_bounded(
            self,
            *,
            resolve_secrets: bool = True,
            statement_timeout_ms: int,
            lock_timeout_ms: int,
        ) -> StoredSnapshot:
            captured = self.snapshots[self.index]
            self.read_count += 1
            if self.read_count == 1 + ConfigRuntime._CONFIRMING_READS:
                self.final_read_entered.set()
                assert self.release_final_read.wait(1)
            return captured

    repository = BlockingFinalReadRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()

    bogus = asyncio.create_task(runtime.reconcile_revision(1 << 60))
    assert await asyncio.to_thread(repository.final_read_entered.wait, 1)
    legitimate = asyncio.create_task(runtime.reconcile_revision(1))
    await wait_until(lambda: bool(runtime._pending_revision_requests))
    repository.index = 1
    repository.release_final_read.set()

    bogus_snapshot, legitimate_snapshot = await asyncio.gather(bogus, legitimate)

    assert bogus_snapshot.revision == 0
    assert legitimate_snapshot.revision == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_revision_poll_heals_dead_listener() -> None:
    class PollingRepository(FakeRepository):
        def current_revision(self) -> int:
            return self.snapshots[self.index].revision

    repository = PollingRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    notifications = FakeNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
        revision_poll_interval=0.02,
    )
    await runtime.start()
    listener = runtime._listener_task
    assert listener is not None
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener

    repository.index = 1

    await wait_until(lambda: runtime.capture().snapshot.revision == 1)
    await wait_until(lambda: notifications.connect_count == 2)
    replacement = runtime._listener_task
    assert replacement is not None
    assert replacement is not listener
    assert not replacement.done()
    assert runtime.capture().snapshot.active.ui.enabled is True
    assert runtime.healthy is True
    await runtime.close()


@pytest.mark.asyncio
async def test_revision_poll_ignores_revision_already_being_reconciled() -> None:
    class BlockingPollingRepository(FakeRepository):
        def __init__(self, snapshots: list[StoredSnapshot]) -> None:
            super().__init__(snapshots)
            self.block_reads = False
            self.revision_polled = threading.Event()
            self.read_entered = threading.Event()
            self.release_read = threading.Event()

        def current_revision(self) -> int:
            self.revision_polled.set()
            return self.snapshots[self.index].revision

        def read_bounded(
            self,
            *,
            resolve_secrets: bool = True,
            statement_timeout_ms: int,
            lock_timeout_ms: int,
        ) -> StoredSnapshot:
            captured = self.snapshots[self.index]
            self.read_count += 1
            if self.block_reads:
                self.read_entered.set()
                assert self.release_read.wait(1)
            return captured

    repository = BlockingPollingRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    notifications = FakeNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
        revision_poll_interval=0.02,
    )
    await runtime.start()
    repository.index = 1
    repository.block_reads = True

    reconcile = asyncio.create_task(runtime.reconcile_revision(1))
    assert await asyncio.to_thread(repository.read_entered.wait, 1)
    repository.revision_polled.clear()
    assert await asyncio.to_thread(repository.revision_polled.wait, 1)

    assert notifications.connect_count == 1
    assert notifications.close_count == 0
    repository.release_read.set()
    assert (await reconcile).revision == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_revision_poll_has_single_reconnect_owner() -> None:
    class PollingRepository(FakeRepository):
        poll_count = 0

        def current_revision(self) -> int:
            self.poll_count += 1
            return self.snapshots[self.index].revision

    class ClosingNotificationSource(FakeNotificationSource):
        async def close(self) -> None:
            trigger_listener = self.close_count == 0
            await super().close()
            if trigger_listener:
                await self.queue.put(ConnectionError("poll closed listener"))

    repository = PollingRepository(
        [
            stored_snapshot(0),
            stored_snapshot(1, ui_enabled=True, changed={"ui.enabled": 1}),
        ]
    )
    notifications = ClosingNotificationSource()
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        notification_source=notifications,
        revision_poll_interval=0.02,
        reconnect_backoff=0,
    )
    await runtime.start()
    repository.index = 1

    await wait_until(lambda: runtime.capture().snapshot.revision == 1)
    await wait_until(lambda: notifications.connect_count == 2)
    poll_count = repository.poll_count
    await wait_until(lambda: repository.poll_count >= poll_count + 2)

    assert notifications.connect_count == 2
    assert runtime.healthy is True
    await runtime.close()
