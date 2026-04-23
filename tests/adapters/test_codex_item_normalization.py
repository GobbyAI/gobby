"""Targeted tests for Codex item normalization helpers."""

from __future__ import annotations

import logging

import pytest

from gobby.adapters.codex_impl.item_normalization import parse_mcp_arguments

pytestmark = pytest.mark.unit


def test_parse_mcp_arguments_logs_debug_on_invalid_json(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.codex_impl.item_normalization"):
        parsed = parse_mcp_arguments('{"broken"')

    assert parsed == {}
    assert any("Failed to parse MCP arguments JSON" in message for message in caplog.messages)
