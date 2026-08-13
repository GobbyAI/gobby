"""Immutable desired/active configuration runtime models."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType

from gobby.config.app import DaemonConfig


@dataclass(frozen=True, slots=True)
class RuntimeSecretBinding:
    """Private last-good secret payload captured with a content fingerprint."""

    reference: str
    plaintext: str | None = field(repr=False)
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ApplyFailure:
    """Local activation failure for one committed revision."""

    revision: int
    subscriber: str
    keys: frozenset[str]
    message: str


@dataclass(frozen=True, slots=True, init=False)
class ConfigSnapshot:
    """One deeply immutable desired/active configuration epoch."""

    revision: int
    row_revisions: MappingProxyType[str, int]
    pending_restart_keys: frozenset[str]
    failed_live_keys: MappingProxyType[str, ApplyFailure]
    _desired: DaemonConfig = field(repr=False, compare=False)
    _active: DaemonConfig = field(repr=False, compare=False)
    _desired_values: MappingProxyType[str, object] = field(repr=False, compare=False)
    _active_values: MappingProxyType[str, object] = field(repr=False, compare=False)
    _desired_overrides: MappingProxyType[str, object] = field(repr=False, compare=False)
    _active_overrides: MappingProxyType[str, object] = field(repr=False, compare=False)
    _desired_bindings: MappingProxyType[str, RuntimeSecretBinding] = field(
        repr=False, compare=False
    )
    _active_bindings: MappingProxyType[str, RuntimeSecretBinding] = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        revision: int,
        desired: DaemonConfig,
        active: DaemonConfig,
        row_revisions: Mapping[str, int],
        pending_restart_keys: frozenset[str],
        failed_live_keys: Mapping[str, ApplyFailure],
        desired_values: Mapping[str, object] | None = None,
        active_values: Mapping[str, object] | None = None,
        desired_overrides: Mapping[str, object] | None = None,
        active_overrides: Mapping[str, object] | None = None,
        desired_bindings: Mapping[str, RuntimeSecretBinding] | None = None,
        active_bindings: Mapping[str, RuntimeSecretBinding] | None = None,
    ) -> None:
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "row_revisions", MappingProxyType(dict(row_revisions)))
        object.__setattr__(self, "pending_restart_keys", pending_restart_keys)
        object.__setattr__(
            self,
            "failed_live_keys",
            MappingProxyType(dict(failed_live_keys)),
        )
        object.__setattr__(self, "_desired", desired.model_copy(deep=True))
        object.__setattr__(self, "_active", active.model_copy(deep=True))
        object.__setattr__(
            self,
            "_desired_values",
            MappingProxyType(dict(desired_values or {})),
        )
        object.__setattr__(
            self,
            "_active_values",
            MappingProxyType(dict(active_values or {})),
        )
        object.__setattr__(
            self,
            "_desired_overrides",
            MappingProxyType(deepcopy(dict(desired_overrides or {}))),
        )
        object.__setattr__(
            self,
            "_active_overrides",
            MappingProxyType(deepcopy(dict(active_overrides or {}))),
        )
        object.__setattr__(
            self,
            "_desired_bindings",
            MappingProxyType(dict(desired_bindings or {})),
        )
        object.__setattr__(
            self,
            "_active_bindings",
            MappingProxyType(dict(active_bindings or {})),
        )

    @property
    def desired(self) -> DaemonConfig:
        """Return an isolated typed desired projection."""
        return self._desired.model_copy(deep=True)

    @property
    def active(self) -> DaemonConfig:
        """Return an isolated typed active projection."""
        return self._active.model_copy(deep=True)

    @property
    def desired_values(self) -> Mapping[str, object]:
        return self._desired_values

    @property
    def active_values(self) -> Mapping[str, object]:
        return self._active_values

    @property
    def desired_overrides(self) -> Mapping[str, object]:
        return MappingProxyType(deepcopy(dict(self._desired_overrides)))

    @property
    def active_overrides(self) -> Mapping[str, object]:
        return MappingProxyType(deepcopy(dict(self._active_overrides)))

    def _read_active_values(self) -> Mapping[str, object]:
        return self._active_values

    def _read_active_overrides(self) -> Mapping[str, object]:
        return self._active_overrides

    def _read_active_bindings(self) -> Mapping[str, RuntimeSecretBinding]:
        return self._active_bindings

    def desired_secret(self, key: str) -> str | None:
        binding = self._desired_bindings.get(key)
        return None if binding is None else binding.plaintext

    def active_secret(self, key: str) -> str | None:
        binding = self._active_bindings.get(key)
        return None if binding is None else binding.plaintext

    def desired_secret_fingerprint(self, key: str) -> str | None:
        binding = self._desired_bindings.get(key)
        return None if binding is None else binding.fingerprint

    def active_secret_fingerprint(self, key: str) -> str | None:
        binding = self._active_bindings.get(key)
        return None if binding is None else binding.fingerprint


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """Candidate revision supplied to prepared subscribers."""

    revision: int
    changed_keys: frozenset[str]
    previous: ConfigSnapshot | None
    desired: DaemonConfig
    _desired_bindings: MappingProxyType[str, RuntimeSecretBinding] = field(
        repr=False, compare=False
    )
    managed: MappingProxyType[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def desired_secret(self, key: str) -> str | None:
        binding = self._desired_bindings.get(key)
        return None if binding is None else binding.plaintext


@dataclass(frozen=True, slots=True)
class UnavailableService:
    """Explicit optional-capability slot published after initial failure."""

    failure: ApplyFailure
