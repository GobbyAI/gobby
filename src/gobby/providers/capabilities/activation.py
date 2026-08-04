"""Allowlisted activation handlers for provider model routes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from gobby.providers.capabilities.models import ActivationDescriptor


class ActivationValidationError(ValueError):
    """Raised when a route activation is unsafe or unsupported."""


@dataclass(frozen=True)
class ActivationHandler:
    """Declarative surface and parameter policy for one activation kind."""

    surfaces: frozenset[str]
    allowed_params: frozenset[str]


_FORBIDDEN_PARAM_FRAGMENTS = ("exec", "env", "path")
_HANDLERS: dict[str, ActivationHandler] = {}


def register_activation_handler(kind: str, handler: ActivationHandler) -> None:
    """Register a new activation kind without changing snapshot schema."""
    if not kind or kind.strip() != kind:
        raise ActivationValidationError("Activation kind must be a non-empty normalized string")
    if kind in _HANDLERS:
        raise ActivationValidationError(f"Activation kind {kind!r} is already registered")
    if not handler.surfaces:
        raise ActivationValidationError("Activation handler must allow at least one surface")
    _reject_forbidden_names(handler.allowed_params)
    _HANDLERS[kind] = handler


def validate_activation(descriptor: ActivationDescriptor) -> ActivationDescriptor:
    """Validate an activation against its registered surface and parameter policy."""
    handler = _HANDLERS.get(descriptor.kind)
    if handler is None:
        raise ActivationValidationError(f"Unknown activation kind: {descriptor.kind!r}")
    if descriptor.surface not in handler.surfaces:
        raise ActivationValidationError(
            f"Activation kind {descriptor.kind!r} does not support surface {descriptor.surface!r}"
        )

    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in descriptor.params.items()
    ):
        raise ActivationValidationError("Activation parameter names and values must be strings")

    parameter_names = frozenset(descriptor.params)
    disallowed = parameter_names - handler.allowed_params
    if disallowed:
        raise ActivationValidationError(
            f"Activation kind {descriptor.kind!r} does not allow parameters: "
            f"{', '.join(sorted(disallowed))}"
        )
    _reject_forbidden_names(parameter_names)

    selector_name = descriptor.params.get("key") or descriptor.params.get("name")
    if selector_name is not None:
        _reject_forbidden_names((selector_name,))
    return descriptor


def _reject_forbidden_names(names: Iterable[str]) -> None:
    for name in names:
        normalized = name.casefold()
        if any(fragment in normalized for fragment in _FORBIDDEN_PARAM_FRAGMENTS):
            raise ActivationValidationError(f"Unsafe activation parameter: {name!r}")


register_activation_handler(
    "model_selector",
    ActivationHandler(
        surfaces=frozenset({"spawn-cli", "app-server", "tool-chat"}),
        allowed_params=frozenset(),
    ),
)
register_activation_handler(
    "cli_config",
    ActivationHandler(
        surfaces=frozenset({"spawn-cli"}),
        allowed_params=frozenset({"key", "value"}),
    ),
)
register_activation_handler(
    "request_parameter",
    ActivationHandler(
        surfaces=frozenset({"app-server", "tool-chat"}),
        allowed_params=frozenset({"name", "value"}),
    ),
)
