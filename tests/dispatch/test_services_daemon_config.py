"""Dispatch reads daemon config from the services' runtime epoch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.config.app import DaemonConfig
from gobby.dispatch.context import services_daemon_config
from tests.config_runtime_helpers import static_runtime_capture

pytestmark = pytest.mark.unit


def test_reads_active_config_through_ready_runtime() -> None:
    config = DaemonConfig(voice={"enabled": True})
    services = SimpleNamespace(
        config_runtime=SimpleNamespace(ready=True, capture=static_runtime_capture(config))
    )

    resolved = services_daemon_config(services)

    assert resolved is not None
    assert resolved.voice.enabled is True


def test_returns_none_without_ready_runtime() -> None:
    unready = SimpleNamespace(
        config_runtime=SimpleNamespace(
            ready=False,
            capture=static_runtime_capture(DaemonConfig()),
        )
    )

    assert services_daemon_config(None) is None
    assert services_daemon_config(SimpleNamespace()) is None
    assert services_daemon_config(SimpleNamespace(config_runtime=None)) is None
    assert services_daemon_config(unready) is None
