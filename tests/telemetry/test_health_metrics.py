"""Tests for bounded logging and automation health metrics."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gobby import telemetry as telemetry_api
from gobby.config.logging import LoggingSettings
from gobby.telemetry import config as telemetry_config
from gobby.telemetry import health_metrics
from gobby.telemetry.health_metrics import (
    configure_health_metrics,
    record_automation_event,
)
from gobby.telemetry.instruments import TelemetryMetrics
from gobby.telemetry.logging import (
    _LoggingMetricHandler,
    get_parser_error_logger,
    setup_file_logging,
)

pytestmark = pytest.mark.unit

MetricKey = tuple[tuple[str, str], ...]


@pytest.fixture
def configured_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Generator[tuple[InMemoryMetricReader, MeterProvider]]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    collector = TelemetryMetrics(provider.get_meter("health-metrics-test"))
    monkeypatch.setattr(health_metrics, "get_telemetry_metrics", lambda: collector)

    setup_file_logging(LoggingSettings(dir=str(tmp_path), level="debug"))
    configure_health_metrics(enabled=True)
    yield reader, provider

    configure_health_metrics(enabled=False)
    provider.shutdown()


def _counter_values(reader: InMemoryMetricReader, name: str) -> dict[MetricKey, int]:
    values: dict[MetricKey, int] = {}
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return values
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                data = cast(Any, metric.data)
                for point in data.data_points:
                    attributes = point.attributes or {}
                    key = tuple(sorted((str(key), str(value)) for key, value in attributes.items()))
                    values[key] = int(point.value)
    return values


def test_logging_handler_counts_each_source_record_once_with_bounded_labels(
    configured_metrics: tuple[InMemoryMetricReader, MeterProvider],
) -> None:
    reader, _ = configured_metrics

    logging.getLogger("gobby.runner").warning("daemon warning")
    logging.getLogger("gobby.hooks.runner").error("hook error")
    logging.getLogger("gobby.ai.text_generation").error("llm error")
    logging.getLogger("gobby.mcp_proxy.tools").critical("mcp critical")
    logging.getLogger("gobby.scheduler.executor").warning("automation warning")
    get_parser_error_logger("codex").error("parser error")
    logging.getLogger("gobby.runner").info("ignored info")
    logging.getLogger("gobby.runner").log(45, "ignored custom level")

    assert _counter_values(reader, "logging_records_total") == {
        (("severity", "CRITICAL"), ("surface", "mcp")): 1,
        (("severity", "ERROR"), ("surface", "hooks")): 1,
        (("severity", "ERROR"), ("surface", "llm")): 1,
        (("severity", "ERROR"), ("surface", "parser")): 1,
        (("severity", "WARNING"), ("surface", "automation")): 1,
        (("severity", "WARNING"), ("surface", "daemon")): 1,
    }


def test_automation_events_use_exact_allowlists_and_increments(
    configured_metrics: tuple[InMemoryMetricReader, MeterProvider],
) -> None:
    reader, _ = configured_metrics

    record_automation_event("cron", "fired", amount=2)
    record_automation_event("dispatcher", "skipped")
    record_automation_event("pipeline-heartbeat", "recovered")
    record_automation_event("cron", "unknown")
    record_automation_event("unknown", "failed")

    assert _counter_values(reader, "automation_events_total") == {
        (("component", "cron"), ("outcome", "fired")): 2,
        (("component", "dispatcher"), ("outcome", "skipped")): 1,
        (("component", "pipeline-heartbeat"), ("outcome", "recovered")): 1,
    }


def test_logging_handler_attachment_is_idempotent(
    configured_metrics: tuple[InMemoryMetricReader, MeterProvider],
) -> None:
    configure_health_metrics(enabled=True)
    configure_health_metrics(enabled=True)

    handlers = [
        handler
        for handler in logging.getLogger("gobby").handlers
        if isinstance(handler, _LoggingMetricHandler)
    ]
    assert len(handlers) == 1


def test_logging_metric_emission_is_non_throwing_and_recursion_safe(
    configured_metrics: tuple[InMemoryMetricReader, MeterProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def recursive_failure(surface: str, severity: str) -> None:
        nonlocal calls
        calls += 1
        logging.getLogger("gobby.telemetry.nested").error("nested metric failure")
        raise RuntimeError(f"failed {surface} {severity}")

    monkeypatch.setattr(health_metrics, "record_logging_record", recursive_failure)

    logging.getLogger("gobby.runner").warning("outer warning")

    assert calls == 1


def test_metrics_disabled_is_complete_no_op(
    configured_metrics: tuple[InMemoryMetricReader, MeterProvider],
) -> None:
    reader, _ = configured_metrics
    configure_health_metrics(enabled=False)

    record_automation_event("cron", "fired")
    logging.getLogger("gobby.runner").warning("disabled warning")

    assert _counter_values(reader, "automation_events_total") == {}
    assert _counter_values(reader, "logging_records_total") == {}
    assert not any(
        isinstance(handler, _LoggingMetricHandler)
        for handler in logging.getLogger("gobby").handlers
    )


def test_init_configures_health_metrics_after_meter_provider_and_file_logging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def get_tracer_provider(_config: object) -> object:
        events.append("tracer-provider")
        return object()

    def get_meter_provider(_config: object) -> object:
        events.append("meter-provider")
        return object()

    def record_event(event: str) -> None:
        events.append(event)

    def setup_logging(_config: object, verbose: bool = False) -> None:
        events.append(f"file-logging:{verbose}")

    def configure_metrics(*, enabled: bool) -> None:
        events.append(f"health-metrics:{enabled}")

    monkeypatch.setattr(
        telemetry_api,
        "get_tracer_provider",
        get_tracer_provider,
    )
    monkeypatch.setattr(
        otel_trace,
        "set_tracer_provider",
        lambda _provider: record_event("set-tracer-provider"),
    )
    monkeypatch.setattr(
        telemetry_api,
        "get_meter_provider",
        get_meter_provider,
    )
    monkeypatch.setattr(
        otel_metrics,
        "set_meter_provider",
        lambda _provider: record_event("set-meter-provider"),
    )
    monkeypatch.setattr(
        telemetry_api,
        "setup_file_logging",
        setup_logging,
    )
    monkeypatch.setattr(
        telemetry_api,
        "configure_health_metrics",
        configure_metrics,
    )

    telemetry_api.init_telemetry(
        telemetry_config.TelemetrySettings(metrics_enabled=True),
        LoggingSettings(dir=str(tmp_path)),
        verbose=True,
    )

    assert events[:2] == ["tracer-provider", "set-tracer-provider"]
    assert events[2:4] == ["meter-provider", "set-meter-provider"]
    assert events[4] == "file-logging:True"
    assert events[5] == "health-metrics:True"
    assert events == [
        "tracer-provider",
        "set-tracer-provider",
        "meter-provider",
        "set-meter-provider",
        "file-logging:True",
        "health-metrics:True",
    ]


@pytest.mark.asyncio
async def test_dispatcher_boundary_records_fatal_failure(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: object,
) -> None:
    from gobby.dispatch import dispatcher

    outcomes: list[tuple[str, str]] = []

    async def fail_heartbeat(**_kwargs: object) -> dispatcher.HeartbeatResult:
        raise RuntimeError("dispatcher failed")

    monkeypatch.setattr(dispatcher, "_run_heartbeat_unlocked", fail_heartbeat)
    monkeypatch.setattr(
        dispatcher,
        "record_automation_event",
        lambda component, outcome: outcomes.append((component, outcome)),
    )

    with pytest.raises(RuntimeError, match="dispatcher failed"):
        await dispatcher.run_heartbeat(db=cast(Any, temp_db))

    assert outcomes == [("dispatcher", "failed")]


@pytest.mark.asyncio
async def test_dispatcher_readiness_block_records_skip(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: object,
) -> None:
    from gobby.agents import readiness
    from gobby.dispatch import dispatcher

    outcomes: list[tuple[str, str]] = []
    monkeypatch.setattr(readiness, "spawn_readiness_blocker", lambda _services: "daemon offline")
    monkeypatch.setattr(
        dispatcher,
        "record_automation_event",
        lambda component, outcome: outcomes.append((component, outcome)),
    )

    result = await dispatcher.run_heartbeat(db=cast(Any, temp_db), services=object())

    assert result.reason == "daemon offline"
    assert outcomes == [("dispatcher", "skipped")]
