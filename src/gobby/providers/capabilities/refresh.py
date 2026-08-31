"""Durable provider capability refresh coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Protocol, cast

from gobby.providers.capabilities.collectors import (
    CapabilityCollector,
    validate_snapshot,
)
from gobby.providers.capabilities.collectors import (
    collectors as registered_collectors,
)
from gobby.providers.capabilities.coverage import CoverageAuditor
from gobby.providers.capabilities.models import ProviderSnapshot
from gobby.providers.capabilities.seed import apply_seed
from gobby.providers.capabilities.store import ProviderCapabilityStore

logger = logging.getLogger(__name__)

CAPABILITY_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60.0
CAPABILITY_SOURCE_TIMEOUT_SECONDS = 30.0
CAPABILITY_REFRESH_DRAIN_TIMEOUT_SECONDS = 7.0


class CapabilityStore(Protocol):
    """Storage operations required by the capability service."""

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None: ...

    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]: ...

    def replace_provider_snapshot(self, snapshot: ProviderSnapshot) -> None: ...

    def record_source_failure(self, provider: str, source_key: str, error: str) -> None: ...


RunDatabase = Callable[..., Awaitable[object]]
Sleep = Callable[[float], Awaitable[None]]


def _default_collectors() -> Mapping[str, CapabilityCollector]:
    from gobby.providers.capabilities.collectors.agy import AgyCollector
    from gobby.providers.capabilities.collectors.claude import ClaudeCollector
    from gobby.providers.capabilities.collectors.codex import CodexCollector
    from gobby.providers.capabilities.collectors.droid import DroidCollector
    from gobby.providers.capabilities.collectors.grok import GrokCollector
    from gobby.providers.capabilities.collectors.qwen import QwenCollector

    builtins = (
        cast(CapabilityCollector, AgyCollector()),
        cast(CapabilityCollector, ClaudeCollector()),
        cast(CapabilityCollector, CodexCollector()),
        cast(CapabilityCollector, DroidCollector()),
        cast(CapabilityCollector, GrokCollector()),
        cast(CapabilityCollector, QwenCollector()),
    )
    defaults = {collector.provider: collector for collector in builtins}
    defaults.update(registered_collectors())
    return defaults


class CapabilityRefreshCoordinator:
    """Serve last-good capabilities and refresh provider snapshots in the background."""

    def __init__(
        self,
        store: CapabilityStore,
        provider_collectors: Mapping[str, CapabilityCollector] | None = None,
        *,
        source_timeout_seconds: float = CAPABILITY_SOURCE_TIMEOUT_SECONDS,
        interval_seconds: float = CAPABILITY_REFRESH_INTERVAL_SECONDS,
        run_db: RunDatabase | None = None,
        sleep: Sleep = asyncio.sleep,
        coverage_auditor: CoverageAuditor | None = None,
    ) -> None:
        self.store = store
        self._collectors = dict(
            _default_collectors() if provider_collectors is None else provider_collectors
        )
        self._source_timeout_seconds = source_timeout_seconds
        self._interval_seconds = interval_seconds
        self._run_db = run_db
        self._sleep = sleep
        self._coverage_auditor = coverage_auditor

    def prepare(self) -> None:
        """Install bundled fallback rows before HTTP starts serving."""
        apply_seed(cast(ProviderCapabilityStore, self.store))
        if self._coverage_auditor is not None:
            try:
                self._coverage_auditor.audit()
            except Exception:
                logger.exception("Provider model metadata coverage audit failed during startup")

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        """Return one durable last-good provider snapshot."""
        return self.store.get_provider_snapshot(provider)

    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        """Return all durable last-good provider snapshots."""
        return self.store.get_all_snapshots()

    async def refresh_all(self) -> None:
        """Refresh every provider concurrently without failing the batch."""
        await asyncio.gather(
            *(self._refresh_provider(collector) for collector in self._collectors.values())
        )

    async def run(self, shutdown_requested: Callable[[], bool]) -> None:
        """Refresh immediately and then every 24 hours until shutdown."""
        while not shutdown_requested():
            await self.refresh_all()
            if shutdown_requested():
                return
            await self._sleep(self._interval_seconds)

    async def _refresh_provider(self, collector: CapabilityCollector) -> None:
        if collector.provider == "agy":
            from gobby.providers.version_gate import ensure_agy_support

            await ensure_agy_support()
        try:
            snapshot = await asyncio.wait_for(
                collector.collect(),
                timeout=self._source_timeout_seconds,
            )
            prior = await self._run_store(
                self.store.get_provider_snapshot,
                collector.provider,
            )
            snapshot = self._with_attempt_counts(snapshot, prior)
            validate_snapshot(snapshot, collector.sources)
            await self._run_store(self.store.replace_provider_snapshot, snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = self._failure_detail(error)
            for source_key in self._failed_source_keys(collector, error):
                await self._run_store(
                    self.store.record_source_failure,
                    collector.provider,
                    source_key,
                    detail,
                )
            log = logger.info if "authentication required" in detail.casefold() else logger.warning
            log("Provider capability refresh failed for %s: %s", collector.provider, detail)
        else:
            await self._audit_coverage(collector.provider)

    async def _audit_coverage(self, provider: str) -> None:
        if self._coverage_auditor is None:
            return
        try:
            await self._coverage_auditor.audit_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Provider model metadata coverage audit failed after %s refresh",
                provider,
            )

    async def _run_store[T](self, operation: Callable[..., T], *args: object) -> T:
        if self._run_db is None:
            return await asyncio.to_thread(operation, *args)
        result = await self._run_db(operation, *args)
        return cast(T, result)

    def _failure_detail(self, error: Exception) -> str:
        if isinstance(error, TimeoutError):
            return f"timed out after {self._source_timeout_seconds:g} seconds"
        return str(error) or type(error).__name__

    @staticmethod
    def _with_attempt_counts(
        snapshot: ProviderSnapshot,
        prior: ProviderSnapshot | None,
    ) -> ProviderSnapshot:
        prior_attempts = (
            {source.source_key: source.attempts for source in prior.sources}
            if prior is not None
            else {}
        )
        sources = tuple(
            replace(
                source,
                attempts=prior_attempts.get(source.source_key, 0) + 1,
            )
            for source in snapshot.sources
        )
        return replace(snapshot, sources=sources)

    @staticmethod
    def _failed_source_keys(
        collector: CapabilityCollector,
        error: Exception,
    ) -> tuple[str, ...]:
        declared = {source.source_key: source for source in collector.sources}
        source_key = getattr(error, "source_key", None)
        if isinstance(source_key, str) and source_key in declared:
            return (source_key,)
        required = tuple(source.source_key for source in collector.sources if source.required)
        return required or tuple(declared)
