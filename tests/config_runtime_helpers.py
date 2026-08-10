from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

from gobby.config.app import DaemonConfig
from gobby.config.cron import CronConfig
from gobby.config.features import KnowledgeGraphQueueConfig
from gobby.config.persistence import MemoryConfig, MemoryDreamConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.config.sessions import SessionLifecycleConfig, SessionSummaryConfig


def static_runtime_capture(config: DaemonConfig) -> Callable[[], RuntimeActiveBundle]:
    snapshot = ConfigSnapshot(
        revision=1,
        desired=config,
        active=config,
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
    )
    bundle = RuntimeActiveBundle(snapshot=snapshot, services=MappingProxyType({}))
    return lambda: bundle


def static_cron_capture(config: CronConfig) -> Callable[[], RuntimeActiveBundle]:
    return static_runtime_capture(DaemonConfig(cron=config))


def static_session_capture(
    config: SessionLifecycleConfig,
    *,
    session_summary: SessionSummaryConfig | None = None,
    kg_queue: KnowledgeGraphQueueConfig | None = None,
    dream: MemoryDreamConfig | None = None,
) -> Callable[[], RuntimeActiveBundle]:
    return static_runtime_capture(
        DaemonConfig(
            session_lifecycle=config,
            session_summary=session_summary or SessionSummaryConfig(),
            knowledge_graph_queue=kg_queue or KnowledgeGraphQueueConfig(),
            memory=MemoryConfig(dream=dream or MemoryDreamConfig()),
        )
    )
