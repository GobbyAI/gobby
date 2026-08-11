"""Build CLI daemon timeout behavior."""

from __future__ import annotations

from typing import Any

import click
import httpx
import pytest

pytestmark = pytest.mark.unit


def test_daemon_build_timeout_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.build import BuildOptions
    from gobby.cli.build import _try_daemon_build

    class TimeoutDaemonClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_health(self) -> tuple[bool, None]:
            return True, None

        def call_http_api(self, *args: object, **kwargs: object) -> Any:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(
        "gobby.utils.daemon_url.daemon_url",
        lambda: "http://127.0.0.1:60887",
    )
    monkeypatch.setattr("gobby.utils.daemon_client.DaemonClient", TimeoutDaemonClient)

    with pytest.raises(click.ClickException) as exc_info:
        _try_daemon_build("#14370", BuildOptions())

    message = str(exc_info.value)
    assert "timed out" in message
    assert "local fallback was skipped" in message


def test_unhealthy_daemon_still_allows_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.build import BuildOptions
    from gobby.cli.build import _try_daemon_build

    class UnhealthyDaemonClient:
        called_http_api = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_health(self) -> tuple[bool, None]:
            return False, None

        def call_http_api(self, *args: object, **kwargs: object) -> Any:
            self.called_http_api = True
            raise AssertionError("unhealthy daemon should not receive build call")

    monkeypatch.setattr(
        "gobby.utils.daemon_url.daemon_url",
        lambda: "http://127.0.0.1:60887",
    )
    monkeypatch.setattr("gobby.utils.daemon_client.DaemonClient", UnhealthyDaemonClient)

    assert _try_daemon_build("#14370", BuildOptions()) is None
    assert UnhealthyDaemonClient.called_http_api is False


def test_daemon_build_payload_carries_project_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build import BuildOptions
    from gobby.cli.build import _try_daemon_build

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "task_id": "7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
                "created": True,
                "initial_lifecycle": "planning",
                "applied_stages_skipped": [],
                "dispatcher_tick": {"scanned": 0, "executed": 0, "skipped": 0},
            }

    class HealthyDaemonClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_health(self) -> tuple[bool, None]:
            return True, None

        def call_http_api(self, *args: object, **kwargs: object) -> FakeResponse:
            calls.append(kwargs["json_data"])
            return FakeResponse()

    monkeypatch.setattr(
        "gobby.utils.daemon_url.daemon_url",
        lambda: "http://127.0.0.1:60887",
    )
    monkeypatch.setattr("gobby.utils.daemon_client.DaemonClient", HealthyDaemonClient)

    result = _try_daemon_build(
        "plan.md",
        BuildOptions(),
        project_id="485d2f9d-67bd-5553-9145-736f9d881382",
        cwd="/tmp/project-2",
    )

    assert result is not None
    assert calls[0]["project_id"] == "485d2f9d-67bd-5553-9145-736f9d881382"
    assert calls[0]["cwd"] == "/tmp/project-2"
