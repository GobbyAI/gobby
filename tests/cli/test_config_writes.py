"""CAS retry helper for installer/CLI configuration writes."""

from __future__ import annotations

from types import MappingProxyType

import click
import pytest

from gobby.cli.config_writes import apply_cas_config_patch
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigValidationError,
)
from gobby.storage.config_repository import ConfigReadSnapshot

pytestmark = pytest.mark.unit


def _snapshot(revision: int) -> ConfigReadSnapshot:
    return ConfigReadSnapshot(
        revision=revision,
        values=MappingProxyType({}),
        overrides=MappingProxyType({}),
        row_revisions=MappingProxyType({}),
        secret_bindings=MappingProxyType({}),
    )


class _Store:
    def __init__(self, outcomes: list[ConfigMutationResult | Exception]) -> None:
        self._outcomes = outcomes
        self.reads = 0
        self.calls: list[tuple[int, ConfigPatch]] = []

    def read_snapshot(self) -> ConfigReadSnapshot:
        self.reads += 1
        return _snapshot(self.reads)

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        self.calls.append((expected_revision, patch))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_success_applies_once() -> None:
    result = ConfigMutationResult(revision=2, changed_keys=frozenset({"voice.enabled"}))
    store = _Store([result])

    applied = apply_cas_config_patch(
        read_snapshot=store.read_snapshot,
        build_patch=lambda snapshot: ConfigPatch(values={"voice.enabled": True}),
        patch=store.patch,
    )

    assert applied is result
    assert store.reads == 1
    assert store.calls[0][0] == 1


def test_conflict_retries_once_from_fresh_snapshot() -> None:
    result = ConfigMutationResult(revision=3, changed_keys=frozenset())
    store = _Store([ConfigConflictError(1, 2), result])
    seen_revisions: list[int] = []

    def build_patch(snapshot: ConfigReadSnapshot) -> ConfigPatch:
        seen_revisions.append(snapshot.revision)
        return ConfigPatch(values={"voice.enabled": True})

    applied = apply_cas_config_patch(
        read_snapshot=store.read_snapshot,
        build_patch=build_patch,
        patch=store.patch,
    )

    assert applied is result
    # The retry re-reads and rebuilds against the fresh epoch.
    assert store.reads == 2
    assert seen_revisions == [1, 2]
    assert [expected for expected, _patch in store.calls] == [1, 2]


def test_second_conflict_raises_distinct_concurrent_message() -> None:
    store = _Store([ConfigConflictError(1, 2), ConfigConflictError(2, 3)])

    with pytest.raises(click.ClickException, match="changed concurrently") as excinfo:
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=lambda snapshot: ConfigPatch(values={"voice.enabled": True}),
            patch=store.patch,
        )

    assert "revision moved to 3" in excinfo.value.message
    assert store.reads == 2


def test_validation_error_never_retries() -> None:
    store = _Store([ConfigValidationError("bad value", key="voice.enabled")])

    with pytest.raises(click.ClickException, match="invalid") as excinfo:
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=lambda snapshot: ConfigPatch(values={"voice.enabled": "nope"}),
            patch=store.patch,
        )

    assert "bad value" in excinfo.value.message
    assert store.reads == 1
    assert len(store.calls) == 1


def test_validation_error_on_retry_is_not_reported_as_conflict() -> None:
    store = _Store(
        [ConfigConflictError(1, 2), ConfigValidationError("bad value", key="voice.enabled")]
    )

    with pytest.raises(click.ClickException, match="invalid"):
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=lambda snapshot: ConfigPatch(values={"voice.enabled": "nope"}),
            patch=store.patch,
        )
