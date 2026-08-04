"""Tests for capability activation descriptor validation."""

from collections.abc import Mapping
from typing import cast

import pytest

from gobby.providers.capabilities.activation import (
    ActivationHandler,
    ActivationValidationError,
    register_activation_handler,
    validate_activation,
)
from gobby.providers.capabilities.models import ActivationDescriptor


def test_unknown_activation_kind_rejected() -> None:
    descriptor = ActivationDescriptor(kind="shell_command", surface="spawn-cli", params={})

    with pytest.raises(ActivationValidationError, match="Unknown activation kind"):
        validate_activation(descriptor)


@pytest.mark.parametrize(
    "params",
    [
        cast(Mapping[str, str], {"key": "model_service_tier", "value": 1}),
        {"key": "model_service_tier", "value": "fast", "env": "TOKEN=secret"},
        {"key": "model_service_tier", "value": "fast", "path": "/tmp/payload"},
        {"key": "model_service_tier", "value": "fast", "exec": "arbitrary"},
    ],
)
def test_activation_params_reject_non_string_and_disallowed_keys(
    params: Mapping[str, str],
) -> None:
    descriptor = ActivationDescriptor(kind="cli_config", surface="spawn-cli", params=params)

    with pytest.raises(ActivationValidationError):
        validate_activation(descriptor)


def test_register_new_handler_kind() -> None:
    register_activation_handler(
        "test_header",
        ActivationHandler(
            surfaces=frozenset({"tool-chat"}),
            allowed_params=frozenset({"name", "value"}),
        ),
    )
    descriptor = ActivationDescriptor(
        kind="test_header",
        surface="tool-chat",
        params={"name": "x-speed", "value": "fast"},
    )

    assert validate_activation(descriptor) is descriptor
