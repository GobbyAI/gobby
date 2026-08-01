"""Regression tests for process-global fixture isolation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from gobby.agents import terminal_delivery
from tests.conftest import _reset_process_global_state


@pytest.mark.asyncio
async def test_reset_process_global_state_clears_providers_and_terminal_work() -> None:
    """A polluted test cannot affect the next serial test."""
    first_tracer_provider = TracerProvider()
    first_meter_provider = MeterProvider()
    trace.set_tracer_provider(first_tracer_provider)
    metrics.set_meter_provider(first_meter_provider)

    leaked_task = MagicMock()
    with patch(
        "gobby.agents.terminal_delivery.asyncio.create_task",
        return_value=leaked_task,
    ):
        await terminal_delivery.shielded_terminal_delivery(
            "polluting-run",
            AsyncMock(return_value=None),
        )

    with pytest.raises(RuntimeError, match="work in flight"):
        terminal_delivery.reopen_terminal_delivery_admission()

    _reset_process_global_state()

    second_tracer_provider = TracerProvider()
    second_meter_provider = MeterProvider()
    trace.set_tracer_provider(second_tracer_provider)
    metrics.set_meter_provider(second_meter_provider)

    assert trace.get_tracer_provider() is second_tracer_provider
    assert metrics.get_meter_provider() is second_meter_provider
    terminal_delivery.reopen_terminal_delivery_admission()
