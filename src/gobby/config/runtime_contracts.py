"""ConfigRuntime integration contract: protocols, errors, and bundle types.

These types define how repositories, registries, subscribers, and notification
sources plug into the runtime, plus the immutable bundle shape the runtime
publishes. The reconciliation engine itself lives in ``gobby.config.runtime``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from gobby.config.app import DaemonConfig
from gobby.config.registry import ActivationPolicy
from gobby.config.runtime_models import ConfigChange, ConfigSnapshot


class StoredSecretBinding(Protocol):
    @property
    def reference(self) -> str: ...

    @property
    def plaintext(self) -> str | None: ...


class StoredConfigSnapshot(Protocol):
    @property
    def revision(self) -> int: ...

    @property
    def values(self) -> Mapping[str, object]: ...

    @property
    def overrides(self) -> Mapping[str, object]: ...

    @property
    def row_revisions(self) -> Mapping[str, int]: ...

    @property
    def secret_bindings(self) -> Mapping[str, StoredSecretBinding]: ...


class ConfigSnapshotRepository(Protocol):
    def read(self, *, resolve_secrets: bool = True) -> StoredConfigSnapshot: ...

    def runtime_candidate(
        self,
        overrides: dict[str, object],
        secret_bindings: Mapping[str, StoredSecretBinding],
    ) -> DaemonConfig: ...


class RegistrySpec(Protocol):
    @property
    def activation(self) -> ActivationPolicy: ...


class RuntimeRegistry(Protocol):
    def resolve(self, key: str) -> RegistrySpec: ...


class PreparedSubscriber(Protocol):
    """Prepared replacement whose reference swap cannot fail."""

    value: object

    def dispose(self) -> None: ...


class ConfigSubscriber(Protocol):
    """One replaceable service driven by a set of live configuration keys."""

    name: str
    keys: frozenset[str]
    required: bool
    prepare_timeout: float
    dispose_timeout: float

    def prepare(self, change: ConfigChange) -> PreparedSubscriber: ...


class ConfigNotificationSource(Protocol):
    async def connect(self) -> None: ...

    def revisions(self) -> AsyncIterator[int]: ...

    async def close(self) -> None: ...


class ConfigRuntimeError(RuntimeError):
    """Base error for runtime activation failures."""


class SecretIdentityMismatchError(ConfigRuntimeError):
    """Raised when a remote daemon has a different KEK/DEK identity."""


class ConstructorLaneSaturatedError(ConfigRuntimeError):
    """Raised when bounded constructor capacity is exhausted."""


@dataclass(frozen=True, slots=True)
class RuntimeActiveBundle:
    """Atomically published snapshot and replaceable service references."""

    snapshot: ConfigSnapshot
    services: MappingProxyType[str, object]
    _handles: MappingProxyType[str, PreparedSubscriber] = field(
        repr=False, compare=False, default_factory=lambda: MappingProxyType({})
    )
    managed: MappingProxyType[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def _read_handles(self) -> Mapping[str, PreparedSubscriber]:
        return self._handles


@dataclass(frozen=True, slots=True)
class PreparedValue:
    """Simple prepared replacement for subscribers without custom cleanup."""

    value: object
    disposer: Callable[[], None] = field(repr=False, compare=False, default=lambda: None)

    def dispose(self) -> None:
        self.disposer()
