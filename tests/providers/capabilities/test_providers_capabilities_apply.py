from __future__ import annotations

import pytest

from gobby.providers.capabilities.apply import (
    SpeedUnavailableError,
    apply_speed,
    finalize_speed,
    speed_result,
)
from gobby.providers.capabilities.models import ActivationDescriptor, SpeedMode
from gobby.providers.capabilities.resolve import SpeedResolution, SpeedStatus

pytestmark = pytest.mark.unit


def _resolution(
    *,
    selector: str = "model-fast",
    status: SpeedStatus = SpeedStatus.FAST_CONFIGURED,
    activations: tuple[ActivationDescriptor, ...] = (),
    reason: str | None = None,
) -> SpeedResolution:
    effective = SpeedMode.STANDARD if status is SpeedStatus.FAST_UNAVAILABLE else SpeedMode.FAST
    return SpeedResolution(
        requested=SpeedMode.FAST,
        effective=effective,
        status=status,
        selector=selector,
        activations=activations,
        reason=reason,
    )


def test_activation_application_per_surface() -> None:
    spawn = apply_speed(
        _resolution(
            activations=(
                ActivationDescriptor("model_selector", "spawn-cli", {}),
                ActivationDescriptor(
                    "cli_config",
                    "spawn-cli",
                    {"key": "model_service_tier", "value": '"fast"'},
                ),
                ActivationDescriptor(
                    "cli_config",
                    "spawn-cli",
                    {"key": "latency", "value": '"low"'},
                ),
            )
        ),
        model="model-standard",
        codex_config_overrides=("existing=true",),
    )
    app_server = apply_speed(
        _resolution(
            activations=(
                ActivationDescriptor("model_selector", "app-server", {}),
                ActivationDescriptor(
                    "request_parameter",
                    "app-server",
                    {"name": "serviceTier", "value": "fast"},
                ),
            )
        ),
        model="model-standard",
        request_parameters={"stream": True},
    )
    tool_chat = apply_speed(
        _resolution(
            activations=(
                ActivationDescriptor(
                    "request_parameter",
                    "tool-chat",
                    {"name": "service_tier", "value": "fast"},
                ),
            )
        ),
        model="model-standard",
    )

    assert spawn.model == "model-fast"
    assert spawn.codex_config_overrides == (
        "existing=true",
        'model_service_tier="fast"',
        'latency="low"',
    )
    assert dict(spawn.request_parameters) == {}
    assert app_server.model == "model-fast"
    assert dict(app_server.request_parameters) == {"stream": True, "serviceTier": "fast"}
    assert dict(tool_chat.request_parameters) == {"service_tier": "fast"}


def test_fast_degraded_upgrade() -> None:
    resolution = _resolution(activations=(ActivationDescriptor("model_selector", "tool-chat", {}),))
    response_metadata: dict[str, object] = {
        "model": "model-standard",
        "output": "completed response",
    }

    finalized = finalize_speed(resolution, response_metadata)

    assert finalized.status is SpeedStatus.FAST_DEGRADED
    assert finalized.effective is SpeedMode.STANDARD
    assert finalized.reason == (
        "provider reported model='model-standard'; expected fast route 'model-fast'"
    )
    assert response_metadata["output"] == "completed response"
    assert speed_result(finalized) == {
        "requested": "fast",
        "effective": "standard",
        "status": "fast_degraded",
        "reason": finalized.reason,
    }


def test_fast_confirmation_requires_provider_echo() -> None:
    resolution = _resolution(
        activations=(
            ActivationDescriptor(
                "request_parameter",
                "app-server",
                {"name": "serviceTier", "value": "fast"},
            ),
        )
    )

    configured = finalize_speed(resolution, {})
    applied = finalize_speed(resolution, {"serviceTier": "fast"})

    assert configured is resolution
    assert applied.status is SpeedStatus.FAST_APPLIED
    assert applied.effective is SpeedMode.FAST
    assert applied.reason is None


def test_fast_unavailable_no_dispatch() -> None:
    resolution = _resolution(
        selector="model-standard",
        status=SpeedStatus.FAST_UNAVAILABLE,
        reason="model has no available fast route",
    )

    try:
        apply_speed(
            resolution,
            model="model-standard",
            codex_config_overrides=("existing=true",),
            request_parameters={"stream": True},
        )
    except SpeedUnavailableError as error:
        assert error.resolution is resolution
        assert error.speed == {
            "requested": "fast",
            "effective": "standard",
            "status": "fast_unavailable",
            "reason": "model has no available fast route",
        }
    else:
        raise AssertionError("fast_unavailable must stop before dispatch")
