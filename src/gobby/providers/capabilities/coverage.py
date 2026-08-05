"""Audit provider context-window coverage against OpenRouter metadata."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Protocol

from gobby.config.ai import model_metadata_alias_source_key
from gobby.llm.context_window_values import positive_context_window
from gobby.providers.capabilities.metadata_aliases import (
    AliasConfigReader,
    load_model_metadata_aliases,
)
from gobby.providers.capabilities.models import ProviderSnapshot

logger = logging.getLogger(__name__)

_WARNING_MODEL_LIMIT = 10
RunDatabase = Callable[..., Awaitable[object]]
ExcludedModels = Callable[[], frozenset[tuple[str, str]]]


def _no_excluded_models() -> frozenset[tuple[str, str]]:
    return frozenset()


class _CapabilityStore(Protocol):
    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]: ...


class _ModelMetadataStore(Protocol):
    def get_context_window(self, model: str) -> int | None: ...


class CoverageAuditor(Protocol):
    def audit(self) -> None: ...

    async def audit_async(self) -> None: ...


class ModelMetadataCoverageAuditor:
    """Log provider metadata gaps only when their bounded sets change."""

    def __init__(
        self,
        capability_store: _CapabilityStore,
        model_metadata_store: _ModelMetadataStore,
        config_store: AliasConfigReader,
        *,
        run_db: RunDatabase | None = None,
        excluded_models: ExcludedModels | None = None,
    ) -> None:
        self._capability_store = capability_store
        self._model_metadata_store = model_metadata_store
        self._config_store = config_store
        self._run_db = run_db
        self._excluded_models = excluded_models or _no_excluded_models
        self._lock = Lock()
        self._unresolved: dict[str, frozenset[str]] = {}
        self._missing_targets: dict[str, frozenset[str]] = {}

    def audit(self) -> None:
        """Compare current provider identities with registry metadata."""
        with self._lock:
            aliases = {
                (alias.provider, alias.provider_model_id): alias.openrouter_model_id
                for alias in load_model_metadata_aliases(self._config_store)
            }
            excluded_models = {
                model_metadata_alias_source_key(provider, model)
                for provider, model in self._excluded_models()
            }
            unresolved: dict[str, frozenset[str]] = {}
            missing_targets: dict[str, frozenset[str]] = {}
            for snapshot in self._capability_store.get_all_snapshots():
                provider_unresolved: set[str] = set()
                provider_missing_targets: set[str] = set()
                for model in snapshot.models:
                    source_key = model_metadata_alias_source_key(
                        snapshot.provider,
                        model.canonical_model,
                    )
                    if source_key in excluded_models:
                        continue
                    if positive_context_window(model.context_length) is not None:
                        continue
                    if (
                        positive_context_window(
                            self._model_metadata_store.get_context_window(model.canonical_model)
                        )
                        is not None
                    ):
                        continue
                    alias_target = aliases.get(source_key)
                    if alias_target is not None:
                        if (
                            positive_context_window(
                                self._model_metadata_store.get_context_window(alias_target)
                            )
                            is not None
                        ):
                            continue
                        provider_missing_targets.add(f"{model.canonical_model} -> {alias_target}")
                    provider_unresolved.add(model.canonical_model)
                if provider_unresolved:
                    unresolved[snapshot.provider] = frozenset(provider_unresolved)
                if provider_missing_targets:
                    missing_targets[snapshot.provider] = frozenset(provider_missing_targets)

            self._report_unresolved(unresolved)
            self._report_missing_targets(missing_targets)
            self._unresolved = unresolved
            self._missing_targets = missing_targets

    async def audit_async(self) -> None:
        """Run a coverage audit without blocking the daemon event loop."""
        if self._run_db is not None:
            await self._run_db(self.audit)
            return
        await asyncio.to_thread(self.audit)

    def _report_unresolved(self, current: dict[str, frozenset[str]]) -> None:
        for provider in sorted(set(current) | set(self._unresolved)):
            values = current.get(provider, frozenset())
            previous = self._unresolved.get(provider, frozenset())
            if values == previous:
                continue
            if values:
                listed, omitted = self._bounded(values)
                logger.warning(
                    "Provider %s has %s models without context metadata: %s%s",
                    provider,
                    len(values),
                    ", ".join(listed),
                    f"; {omitted} omitted" if omitted else "",
                )
            elif previous:
                logger.info("Provider %s context metadata coverage recovered", provider)

    def _report_missing_targets(self, current: dict[str, frozenset[str]]) -> None:
        for provider in sorted(set(current) | set(self._missing_targets)):
            values = current.get(provider, frozenset())
            previous = self._missing_targets.get(provider, frozenset())
            if values == previous:
                continue
            if values:
                listed, omitted = self._bounded(values)
                logger.warning(
                    "Provider %s has %s configured alias targets missing from model_metadata: %s%s",
                    provider,
                    len(values),
                    ", ".join(listed),
                    f"; {omitted} omitted" if omitted else "",
                )
            elif previous:
                logger.info("Provider %s model metadata alias targets recovered", provider)

    @staticmethod
    def _bounded(values: frozenset[str]) -> tuple[tuple[str, ...], int]:
        ordered = tuple(sorted(values))
        return ordered[:_WARNING_MODEL_LIMIT], max(0, len(ordered) - _WARNING_MODEL_LIMIT)
