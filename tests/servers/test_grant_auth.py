"""Grant presentation helpers."""

from __future__ import annotations

import pytest

from gobby.servers.grant_auth import AuthDecision, present_or_reject

pytestmark = pytest.mark.unit


def test_present_or_reject_does_not_leak_decode_errors() -> None:
    raw = "%%%not-a-grant%%%"
    decision = present_or_reject(object(), raw, now=0, required=None)
    assert isinstance(decision, AuthDecision)
    assert decision.allowed is False
    assert decision.code == "invalid_signature"
    assert decision.message == "invalid grant"
    assert raw not in (decision.message or "")
