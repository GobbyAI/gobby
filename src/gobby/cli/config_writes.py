"""Shared CAS helper for installer/CLI configuration writes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import click

from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigValidationError,
)
from gobby.storage.config_repository import ConfigReadSnapshot


class CasPatch(Protocol):
    def __call__(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult: ...


def apply_cas_config_patch(
    *,
    read_snapshot: Callable[[], ConfigReadSnapshot],
    build_patch: Callable[[ConfigReadSnapshot], ConfigPatch],
    patch: CasPatch,
) -> ConfigMutationResult:
    """Apply one CAS config patch, retrying once when the revision moved.

    ``build_patch`` runs against each freshly read snapshot so a retry never
    replays state derived from the stale epoch. Validation failures never
    retry; a second conflict surfaces as a concurrent-change error.
    """

    def attempt() -> ConfigMutationResult:
        snapshot = read_snapshot()
        return patch(expected_revision=snapshot.revision, patch=build_patch(snapshot))

    try:
        try:
            return attempt()
        except ConfigConflictError:
            return attempt()
    except ConfigValidationError as exc:
        raise click.ClickException(f"Configuration update is invalid: {exc}") from exc
    except ConfigConflictError as exc:
        raise click.ClickException(
            "Configuration changed concurrently while writing "
            f"(revision moved to {exc.actual_revision}); re-run the command."
        ) from exc
