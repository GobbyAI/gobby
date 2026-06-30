"""Tests for CLI daemon client resolver helpers."""

import logging

import pytest

from gobby.cli.utils_config import get_daemon_client, get_daemon_url

pytestmark = pytest.mark.unit


def test_get_daemon_url_uses_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://daemon.example.test:61999/")

    assert get_daemon_url() == "http://daemon.example.test:61999"


def test_get_daemon_client_dials_resolved_url(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("test.daemon-client")
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://daemon.example.test:61999/")

    client = get_daemon_client(timeout=12.5, logger=logger)

    assert client.url == "http://daemon.example.test:61999"
    assert client.timeout == 12.5
    assert client.logger is logger
