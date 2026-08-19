"""CLI coverage for single-active-daemon lease control."""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli.daemon_lease import lease


def _response(status_code: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "http://daemon/api/admin/lease"),
    )


def test_promote_calls_standby_control_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.call_http_api.return_value = _response(200, {"promoting": True})
    monkeypatch.setattr("gobby.cli.daemon_lease.get_daemon_client", lambda: client)

    result = CliRunner().invoke(lease, ["promote"])

    assert result.exit_code == 0
    assert "Promotion accepted" in result.output
    client.call_http_api.assert_called_once_with("/api/admin/lease/promote")


def test_handoff_reports_blockers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.call_http_api.return_value = _response(
        409,
        {"detail": {"blockers": {"active_agent_runs": 2}}},
    )
    monkeypatch.setattr("gobby.cli.daemon_lease.get_daemon_client", lambda: client)

    result = CliRunner().invoke(lease, ["handoff"])

    assert result.exit_code != 0
    assert "active_agent_runs" in result.output


def test_recover_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    monkeypatch.setattr("gobby.cli.daemon_lease.get_daemon_client", lambda: client)

    result = CliRunner().invoke(lease, ["recover"], input="n\n")

    assert result.exit_code != 0
    client.call_http_api.assert_not_called()
