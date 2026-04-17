"""Unit tests for shared tool approval helpers."""

from __future__ import annotations

import pytest

from gobby.servers.tool_approvals import (
    is_builtin_auto_exempt,
    normalize_stored_approval_key,
)

pytestmark = pytest.mark.unit


def test_normalize_stored_approval_key_ignores_malformed_call_tool_key() -> None:
    assert normalize_stored_approval_key("call_tool:legacy") == ""


def test_is_builtin_auto_exempt_allows_known_gobby_servers() -> None:
    assert is_builtin_auto_exempt("mcp__gobby__do_thing", {})


def test_is_builtin_auto_exempt_rejects_unknown_gobby_like_server() -> None:
    assert not is_builtin_auto_exempt("mcp__gobby-evil__do_thing", {})
