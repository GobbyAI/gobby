"""Run-mode gating of the periodic maintenance tasks."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.provider_capabilities import ProviderCapabilitiesConfig
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_periodic import _default_loops, start_periodic_tasks
from tests.config_runtime_helpers import static_runtime_capture

HUB_ONLY = frozenset(
    {
        "metrics-cleanup",
        "test-schema-sweep",
        "tool-result-cleanup",
        "workflow-audit-cleanup",
        "metrics-archive",
        "model-metadata-refresh",
        "provider-capability-refresh",
        "span-cleanup",
        "unmodeled-observation-cleanup",
        "loop-progress-cleanup",
        "memory-reconcile",
        "zombie-message-cleanup",
        "comms-message-cleanup",
        "skill-retention-purge",
        "chat-attachment-cleanup",
        "hook-receipt-retention",
        "approval-timeout-expiry",
        "metric-snapshot",
    }
)
MACHINE_LOCAL = frozenset(
    {
        "resource-monitor",
        "hook-inbox-drain",
        "hook-quarantine-retention",
        "bin-freshness",
        "expired-isolation-cleanup",
        "tmux-window-repair",
        "generation-endpoint-health",
    }
)


async def _complete(*_args: Any, **_kwargs: Any) -> None:
    return None


class _RefreshService:
    def run(self, _is_shutdown: Any) -> Any:
        return _complete()


def _runner(bootstrap_config: BootstrapConfig) -> SimpleNamespace:
    return SimpleNamespace(
        bootstrap_config=bootstrap_config,
        config_runtime=SimpleNamespace(capture=static_runtime_capture(DaemonConfig())),
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        db_executor=None,
        memory_manager=object(),
        http_server=SimpleNamespace(
            app=object(),
            services=SimpleNamespace(
                model_metadata_coverage_auditor=None,
                provider_capability_service=_RefreshService(),
                generation_endpoint_health=_RefreshService(),
                startup_ready=True,
            ),
        ),
        session_manager=None,
        degraded_services=set(),
        _shutdown_requested=False,
    )


async def _started_task_names(runner: SimpleNamespace) -> set[str]:
    loops = dict.fromkeys(_default_loops(), _complete)
    start_periodic_tasks(cast(GobbyRunner, runner), tracker=None, **loops)
    tasks = [task for task in vars(runner).values() if isinstance(task, asyncio.Task)]
    await asyncio.gather(*tasks)
    return {task.get_name() for task in tasks}


def _skip_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return sorted(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("skipping hub-only ")
    )


@pytest.mark.asyncio
async def test_node_mode_skips_hub_only_periodic_tasks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = _runner(BootstrapConfig(datastore_mode="remote"))

    with caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle_periodic"):
        started = await _started_task_names(runner)

    assert started == MACHINE_LOCAL
    assert _skip_messages(caplog) == sorted(
        f"skipping hub-only {name} in node mode" for name in HUB_ONLY
    )
    assert runner._metric_snapshot_task is None
    assert runner._approval_timeout_task is None
    # The endpoint probe is this machine's in-memory diagnostic; a node keeps it.
    assert runner._generation_endpoint_health_task is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("hub", [False, True], ids=["standalone", "hub"])
async def test_standalone_and_hub_start_every_periodic_task(
    hub: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = _runner(BootstrapConfig(hub=hub))

    with caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle_periodic"):
        started = await _started_task_names(runner)

    assert started == HUB_ONLY | MACHINE_LOCAL
    assert _skip_messages(caplog) == []


@pytest.mark.asyncio
async def test_disabled_capability_refresh_launches_no_provider_clis() -> None:
    runner = _runner(BootstrapConfig())
    config = DaemonConfig(provider_capabilities=ProviderCapabilitiesConfig(refresh_enabled=False))
    runner.config_runtime = SimpleNamespace(capture=static_runtime_capture(config))

    started = await _started_task_names(runner)

    assert started == (HUB_ONLY | MACHINE_LOCAL) - {"provider-capability-refresh"}
    assert runner._provider_capability_refresh_task is None
