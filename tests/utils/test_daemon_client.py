"""Focused daemon bearer-auth client tests."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.daemon_client import DaemonClient

pytestmark = pytest.mark.unit

AUTH_HEADERS = {"Authorization": "Bearer test-token"}
AUTH_REMEDIATION = (
    "token missing or stale; run 'gobby install' or 'gobby auth token --rotate' on the hub "
    "machine and copy ~/.gobby/local_cli_token here"
)


def test_auth_headers_attached() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True}

    with patch("gobby.utils.daemon_client.daemon_auth_headers", return_value=AUTH_HEADERS):
        client = DaemonClient()

    with (
        patch("gobby.utils.daemon_client.httpx.get", return_value=response) as mock_get,
        patch("gobby.utils.daemon_client.httpx.post", return_value=response) as mock_post,
        patch("gobby.utils.daemon_client.httpx.put", return_value=response) as mock_put,
        patch("gobby.utils.daemon_client.httpx.delete", return_value=response) as mock_delete,
    ):
        assert client.check_health() == (True, None)
        client.call_http_api("/api/example", method="GET")
        client.call_http_api("/api/example", method="POST", json_data={"key": "value"})
        client.call_http_api("/api/example", method="PUT", json_data={"key": "value"})
        client.call_http_api("/api/example", method="DELETE")

    assert all(call.kwargs["headers"] == AUTH_HEADERS for call in mock_get.call_args_list)
    assert mock_post.call_args.kwargs["headers"] == AUTH_HEADERS
    assert mock_put.call_args.kwargs["headers"] == AUTH_HEADERS
    assert mock_delete.call_args.kwargs["headers"] == AUTH_HEADERS


def test_daemon_client_401_has_actionable_remediation() -> None:
    unauthorized = MagicMock(status_code=401)

    with patch("gobby.utils.daemon_client.daemon_auth_headers", return_value=AUTH_HEADERS):
        client = DaemonClient()

    with patch("gobby.utils.daemon_client.httpx.get", return_value=unauthorized):
        healthy, error = client.check_health()

    assert healthy is False
    assert error is not None
    assert AUTH_REMEDIATION in error

    with (
        patch("gobby.utils.daemon_client.httpx.post", return_value=unauthorized),
        pytest.raises(RuntimeError, match="token missing or stale") as exc_info,
    ):
        client.call_http_api("/api/example", method="POST")

    assert AUTH_REMEDIATION in str(exc_info.value)
