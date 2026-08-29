from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.registry import CONFIG_REGISTRY, ActivationPolicy
from gobby.config.runtime import ConfigRuntime, StoredSecretBinding
from gobby.config.runtime_models import ConfigChange, UnavailableService
from gobby.runner_init.config_subscribers import (
    PreparedService,
    ServiceSubscriber,
    UnmappedLiveConfigKeysError,
    live_consumer_matrix,
    live_subscriber_keys,
)


@dataclass(frozen=True)
class StoredSnapshot:
    revision: int
    values: Mapping[str, object]
    overrides: Mapping[str, object]
    row_revisions: Mapping[str, int]
    secret_bindings: Mapping[str, StoredSecretBinding]


class FakeRepository:
    def __init__(self, snapshots: list[StoredSnapshot]) -> None:
        self.snapshots = snapshots
        self.index = 0

    def read(self, *, resolve_secrets: bool = True) -> StoredSnapshot:
        assert resolve_secrets
        return self.snapshots[self.index]

    def runtime_candidate(
        self, overrides: dict[str, object], _secret_bindings: object
    ) -> DaemonConfig:
        return DaemonConfig()


class FakeSpec:
    activation = ActivationPolicy.LIVE


class FakeRegistry:
    def resolve(self, key: str) -> FakeSpec:
        return FakeSpec()


@dataclass
class Client:
    revision: int
    disposed: list[int]

    def close(self) -> None:
        self.disposed.append(self.revision)


def snapshot(revision: int, **values: object) -> StoredSnapshot:
    revisions = dict.fromkeys(values, revision)
    frozen_values = MappingProxyType(values)
    return StoredSnapshot(
        revision=revision,
        values=frozen_values,
        overrides=frozen_values,
        row_revisions=MappingProxyType(revisions),
        secret_bindings=MappingProxyType({}),
    )


def subscriber(
    name: str,
    keys: set[str],
    builder: Callable[[ConfigChange], object],
    *,
    required: bool = True,
    prepare_timeout: float = 0.2,
    dispose_timeout: float = 0.2,
) -> ServiceSubscriber:
    return ServiceSubscriber(
        name=name,
        keys=keys,
        builder=builder,
        required=required,
        prepare_timeout=prepare_timeout,
        dispose_timeout=dispose_timeout,
    )


@pytest.mark.asyncio
async def test_prepare_precedes_every_swap() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    observations: list[tuple[str, int]] = []
    runtime: ConfigRuntime

    def build(name: str) -> Callable[[ConfigChange], object]:
        def prepare(change: ConfigChange) -> object:
            active_revision = -1 if change.previous is None else runtime.capture().snapshot.revision
            observations.append((name, active_revision))
            return change.revision

        return prepare

    subscribers = [
        subscriber("first", {"alpha"}, build("first")),
        subscriber("second", {"alpha"}, build("second")),
    ]
    runtime = ConfigRuntime(repository, registry=FakeRegistry(), subscribers=subscribers)
    await runtime.start()
    observations.clear()
    repository.index = 1

    await runtime.reconcile_revision(1)

    assert observations == [("first", 0), ("second", 0)]
    assert runtime.capture().snapshot.revision == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_prepare_keeps_last_good_services() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    disposed: list[int] = []

    def build(change: ConfigChange) -> PreparedService:
        if change.revision == 1:
            raise RuntimeError("broken replacement")
        client = Client(change.revision, disposed)
        return PreparedService(client, client.close)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build)],
    )
    await runtime.start()
    previous = runtime.capture().services["client"]
    repository.index = 1

    await runtime.reconcile_revision(1)

    assert runtime.capture().services["client"] is previous
    assert "alpha" in runtime.snapshot.failed_live_keys
    assert disposed == []
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_activation_preserves_last_good_bundle() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    disposed: list[int] = []

    def build(change: ConfigChange) -> PreparedService:
        client = Client(change.revision, disposed)

        def activate() -> None:
            if change.revision == 1:
                raise RuntimeError("broken activation")

        return PreparedService(client, client.close, activate)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build)],
    )
    await runtime.start()
    previous_bundle = runtime.capture()
    previous_service = previous_bundle.services["client"]
    previous_handle = previous_bundle._handles["client"]
    repository.index = 1

    with pytest.raises(RuntimeError, match="broken activation"):
        await runtime.reconcile_revision(1)

    bundle = runtime.capture()
    failure = bundle.snapshot.failed_live_keys["alpha"]
    assert bundle.snapshot.revision == 1
    assert failure.subscriber == "client"
    assert failure.message == "broken activation"
    assert bundle.snapshot.active_values["alpha"] == 0
    assert bundle.services["client"] is previous_service
    assert bundle._handles["client"] is previous_handle
    assert disposed == [1]
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_reprepare_activation_preserves_last_good_bundle() -> None:
    repository = FakeRepository([snapshot(0, alpha=0)])
    created: list[Client] = []
    disposed: list[Client] = []
    broken = False

    def build(change: ConfigChange) -> PreparedService:
        client = Client(change.revision, [])
        created.append(client)

        def activate() -> None:
            if broken:
                raise RuntimeError("broken reprepare activation")

        return PreparedService(client, lambda: disposed.append(client), activate)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build)],
    )
    await runtime.start()
    previous_bundle = runtime.capture()

    broken = True
    with pytest.raises(RuntimeError, match="broken reprepare activation"):
        await runtime.reprepare_subscriber("client")

    bundle = runtime.capture()
    failure = bundle.snapshot.failed_live_keys["alpha"]
    assert failure.subscriber == "client"
    assert failure.message == "broken reprepare activation"
    assert bundle.snapshot.active_values["alpha"] == 0
    assert bundle.services["client"] is previous_bundle.services["client"]
    assert bundle._handles["client"] is previous_bundle._handles["client"]
    assert disposed == [created[1]]
    await runtime.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reprepare_subscriber_retries_failed_prepare_at_same_revision() -> None:
    repository = FakeRepository([snapshot(0, alpha=0)])
    disposed: list[int] = []
    broken = False

    def build(change: ConfigChange) -> PreparedService:
        if broken:
            raise RuntimeError("broken replacement")
        client = Client(change.revision, disposed)
        return PreparedService(client, client.close)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build)],
    )
    await runtime.start()
    previous = runtime.capture().services["client"]

    broken = True
    failed = await runtime.reprepare_subscriber("client")

    failure = failed.failed_live_keys["alpha"]
    assert runtime.capture().services["client"] is previous
    assert failure.subscriber == "client"
    assert failure.message == "broken replacement"

    broken = False
    retried = await runtime.reprepare_subscriber("client")

    replacement = cast(Client, runtime.capture().services["client"])
    assert retried.revision == 0
    assert replacement.revision == 0
    assert replacement is not previous
    assert "alpha" not in retried.failed_live_keys
    assert retried.active_values["alpha"] == 0
    assert disposed == [0]
    await runtime.close()


@pytest.mark.asyncio
async def test_successful_swap_drains_old_client() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    disposed: list[int] = []
    lifecycle: list[tuple[str, int]] = []

    def build(change: ConfigChange) -> PreparedService:
        client = Client(change.revision, disposed)

        def dispose() -> None:
            client.close()
            lifecycle.append(("dispose", change.revision))

        return PreparedService(
            client,
            dispose,
            lambda: lifecycle.append(("activate", change.revision)),
        )

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build)],
    )
    await runtime.start()
    lifecycle.clear()
    repository.index = 1

    await runtime.reconcile_revision(1)

    assert cast(Client, runtime.capture().services["client"]).revision == 1
    assert disposed == [0]
    assert lifecycle == [("activate", 1), ("dispose", 0)]
    await runtime.close()


@pytest.mark.asyncio
async def test_key_scoped_invalidation() -> None:
    current = snapshot(0, api_key="old", unrelated=0)
    updated = replace(
        snapshot(1, api_key="new", unrelated=0),
        row_revisions=MappingProxyType({"api_key": 1, "unrelated": 0}),
    )
    repository = FakeRepository([current, updated])
    builds: list[tuple[str, int]] = []

    def build(name: str) -> Callable[[ConfigChange], object]:
        def prepare(change: ConfigChange) -> object:
            builds.append((name, change.revision))
            return object()

        return prepare

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[
            subscriber("dependent", {"api_key"}, build("dependent")),
            subscriber("unrelated", {"unrelated"}, build("unrelated")),
        ],
    )
    await runtime.start()
    builds.clear()
    repository.index = 1

    await runtime.reconcile_revision(1)

    assert builds == [("dependent", 1)]
    await runtime.close()


@pytest.mark.asyncio
async def test_registration_race_resolves_to_latest_revision() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()
    entered = threading.Event()
    release = threading.Event()
    revisions: list[int] = []
    activated: list[int] = []

    def build(change: ConfigChange) -> PreparedService:
        revisions.append(change.revision)
        if change.revision == 0:
            entered.set()
            assert release.wait(timeout=1)
        return PreparedService(
            change.revision,
            _activate=lambda: activated.append(change.revision),
        )

    registration = asyncio.create_task(
        runtime.register_subscriber(subscriber("late", {"alpha"}, build))
    )
    assert await asyncio.to_thread(entered.wait, 1)
    repository.index = 1
    reconciliation = asyncio.create_task(runtime.reconcile_revision(1))
    release.set()

    await asyncio.gather(registration, reconciliation)

    assert runtime.capture().snapshot.revision == 1
    assert runtime.capture().services["late"] == 1
    assert revisions == [0, 1]
    assert activated == [1]
    await runtime.close()


@pytest.mark.asyncio
async def test_preparation_timeout_preserves_last_good() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    release = threading.Event()
    late_disposed = threading.Event()
    disposed: list[int] = []

    def build(change: ConfigChange) -> PreparedService:
        client = Client(change.revision, disposed)
        if change.revision == 1:
            release.wait(timeout=1)

        def dispose() -> None:
            client.close()
            late_disposed.set()

        return PreparedService(client, dispose)

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build, prepare_timeout=0.02)],
    )
    await runtime.start()
    previous = runtime.capture().services["client"]
    repository.index = 1

    await runtime.reconcile_revision(1)
    release.set()
    assert await asyncio.to_thread(late_disposed.wait, 1)

    assert runtime.capture().services["client"] is previous
    assert "alpha" in runtime.snapshot.failed_live_keys
    assert disposed == [1]
    await runtime.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_subscriber_work() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    release = threading.Event()

    def build(change: ConfigChange) -> object:
        if change.revision == 1:
            release.wait(timeout=1)
        return change.revision

    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, build, prepare_timeout=0.02)],
    )
    await runtime.start()
    repository.index = 1
    started = time.monotonic()
    await runtime.reconcile_revision(1)
    await runtime.close()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2
    assert runtime.ready is False


@pytest.mark.asyncio
async def test_no_mixed_epoch_under_interleaving() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    runtime = ConfigRuntime(
        repository,
        registry=FakeRegistry(),
        subscribers=[subscriber("client", {"alpha"}, lambda change: change.revision)],
    )
    await runtime.start()
    old = runtime.capture()
    repository.index = 1

    await runtime.reconcile_revision(1)

    new = runtime.capture()
    assert (old.snapshot.revision, old.services["client"]) == (0, 0)
    assert (new.snapshot.revision, new.services["client"]) == (1, 1)
    await runtime.close()


def test_live_key_consumer_matrix_is_complete() -> None:
    matrix = live_consumer_matrix()
    live_keys = {
        spec.key for spec in CONFIG_REGISTRY.specs if spec.activation is ActivationPolicy.LIVE
    }
    assert {entry.registry_key for entry in matrix} == live_keys
    subscriber_names = {name for entry in matrix for name in entry.subscribers}
    assert subscriber_names == {
        "ai_services",
        "chat_config",
        "code_index",
        "mcp_manager",
        "mcp_proxy_config",
        "memory_services",
        "message_processor",
        "task_validator",
    }
    assert all(live_subscriber_keys(name) for name in subscriber_names)
    synthetic = replace(CONFIG_REGISTRY.key_specs[0], key="unmapped.live.setting")

    with pytest.raises(UnmappedLiveConfigKeysError, match="unmapped.live.setting"):
        live_consumer_matrix((*CONFIG_REGISTRY.specs, synthetic))


def test_session_feedback_survey_is_routed_to_rule_evaluation() -> None:
    entry = next(
        item for item in live_consumer_matrix() if item.registry_key == "session_feedback.survey"
    )
    assert entry.consumers == ("session-feedback survey rules",)
    assert entry.access_path == "per_operation"
    assert entry.subscribers == ()


@pytest.mark.asyncio
async def test_first_registration_failure_contract() -> None:
    repository = FakeRepository([snapshot(0, alpha=0), snapshot(1, alpha=1)])
    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()

    with pytest.raises(RuntimeError, match="required failed"):
        await runtime.register_subscriber(
            subscriber(
                "required",
                {"alpha"},
                lambda _change: (_ for _ in ()).throw(RuntimeError("required failed")),
            )
        )

    attempts = 0

    def optional(change: ConfigChange) -> object:
        nonlocal attempts
        attempts += 1
        if change.revision == 0:
            raise RuntimeError("optional failed")
        return change.revision

    await runtime.register_subscriber(subscriber("optional", {"alpha"}, optional, required=False))
    assert isinstance(runtime.capture().services["optional"], UnavailableService)
    repository.index = 1

    await runtime.reconcile_revision(1)

    assert runtime.capture().services["optional"] == 1
    assert attempts == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_optional_registration_activation_failure_records_unavailable() -> None:
    repository = FakeRepository([snapshot(0, alpha=0)])
    disposed: list[Client] = []

    def build(change: ConfigChange) -> PreparedService:
        client = Client(change.revision, [])

        def activate() -> None:
            raise RuntimeError("optional activation failed")

        return PreparedService(client, lambda: disposed.append(client), activate)

    runtime = ConfigRuntime(repository, registry=FakeRegistry())
    await runtime.start()
    registered = await runtime.register_subscriber(
        subscriber("optional", {"alpha"}, build, required=False)
    )
    bundle = runtime.capture()
    failure = registered.failed_live_keys["alpha"]
    assert isinstance(bundle.services["optional"], UnavailableService)
    assert "optional" not in bundle._handles
    assert failure.subscriber == "optional"
    assert failure.message == "optional activation failed"
    assert len(disposed) == 1
    await runtime.close()
