"""Apply resolved provider speed routes at dispatch boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TypedDict

from gobby.providers.capabilities.models import ActivationDescriptor, SpeedMode
from gobby.providers.capabilities.resolve import SpeedResolution, SpeedStatus


class SpeedResultData(TypedDict):
    """JSON-ready execution result for one speed request."""

    requested: str
    effective: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class SpeedApplication:
    """Concrete request values after ordered route activation."""

    model: str | None
    codex_config_overrides: tuple[str, ...]
    request_parameters: Mapping[str, object]


class SpeedUnavailableError(ValueError):
    """A fast request that has no dispatchable route on its surface."""

    def __init__(self, resolution: SpeedResolution) -> None:
        self.resolution = resolution
        super().__init__(resolution.reason or "fast route is unavailable")

    @property
    def speed(self) -> SpeedResultData:
        """Return the typed result suitable for a transport error payload."""
        return speed_result(self.resolution)


def speed_result(resolution: SpeedResolution) -> SpeedResultData:
    """Serialize a speed resolution for transport result metadata."""
    return {
        "requested": resolution.requested.value,
        "effective": resolution.effective.value,
        "status": resolution.status.value,
        "reason": resolution.reason,
    }


def apply_speed(
    resolution: SpeedResolution,
    *,
    model: str | None,
    codex_config_overrides: tuple[str, ...] = (),
    request_parameters: Mapping[str, object] | None = None,
) -> SpeedApplication:
    """Apply ordered route activations to copies of dispatch request values."""
    if resolution.status is SpeedStatus.FAST_UNAVAILABLE:
        raise SpeedUnavailableError(resolution)

    applied_model = model
    applied_overrides = codex_config_overrides
    applied_parameters = dict(request_parameters or {})

    for activation in resolution.activations:
        kind_surface = (activation.kind, activation.surface)
        if kind_surface in {
            ("model_selector", "spawn-cli"),
            ("model_selector", "app-server"),
            ("model_selector", "tool-chat"),
        }:
            applied_model = resolution.selector
        elif kind_surface == ("cli_config", "spawn-cli"):
            applied_overrides += (f"{activation.params['key']}={activation.params['value']}",)
        elif kind_surface in {
            ("request_parameter", "app-server"),
            ("request_parameter", "tool-chat"),
        }:
            applied_parameters[activation.params["name"]] = activation.params["value"]
        else:
            raise ValueError(
                f"Unsupported speed activation {activation.kind!r} "
                f"for surface {activation.surface!r}"
            )

    return SpeedApplication(
        model=applied_model,
        codex_config_overrides=applied_overrides,
        request_parameters=MappingProxyType(applied_parameters),
    )


def finalize_speed(
    resolution: SpeedResolution,
    response_metadata: Mapping[str, object],
) -> SpeedResolution:
    """Upgrade configured fast status when provider response metadata confirms it."""
    if resolution.status is not SpeedStatus.FAST_CONFIGURED:
        return resolution

    evidence = _confirmation_evidence(resolution, response_metadata)
    if not evidence:
        return resolution

    for field, actual, expected in evidence:
        if actual != expected:
            expected_description = (
                f"fast route {expected!r}" if field == "model" else repr(expected)
            )
            return replace(
                resolution,
                effective=SpeedMode.STANDARD,
                status=SpeedStatus.FAST_DEGRADED,
                reason=(f"provider reported {field}={actual!r}; expected {expected_description}"),
            )

    return replace(resolution, status=SpeedStatus.FAST_APPLIED, reason=None)


def _confirmation_evidence(
    resolution: SpeedResolution,
    response_metadata: Mapping[str, object],
) -> list[tuple[str, object, str]]:
    evidence: list[tuple[str, object, str]] = []
    for activation in resolution.activations:
        field, expected = _expected_echo(resolution, activation)
        for candidate in _echo_candidates(field):
            if candidate in response_metadata:
                evidence.append((candidate, response_metadata[candidate], expected))
                break
    return evidence


def _expected_echo(
    resolution: SpeedResolution,
    activation: ActivationDescriptor,
) -> tuple[str, str]:
    if activation.kind == "model_selector":
        return "model", resolution.selector
    if activation.kind == "cli_config":
        return activation.params["key"], activation.params["value"]
    if activation.kind == "request_parameter":
        return activation.params["name"], activation.params["value"]
    raise ValueError(f"Unsupported speed confirmation activation: {activation.kind!r}")


def _echo_candidates(field: str) -> tuple[str, ...]:
    if "tier" not in field.casefold():
        return (field,)
    return tuple(dict.fromkeys((field, "serviceTier", "service_tier", "tier")))
