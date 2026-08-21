"""Grant presentation helpers."""

from __future__ import annotations

import pytest

from gobby.runtime_grants import RequiredCapability
from gobby.runtime_grants.schema import GrantBundle
from gobby.servers.grant_auth import AuthDecision, present_or_reject

pytestmark = pytest.mark.unit


class _NeverPresenter:
    """GrantPresenter that must not be reached when the header fails to decode."""

    def present(
        self,
        grant: GrantBundle,
        *,
        now: int | None = None,
        required: RequiredCapability | None = None,
    ) -> GrantBundle:
        raise AssertionError("present() must not run for an undecodable grant header")


def test_present_or_reject_does_not_leak_decode_errors() -> None:
    raw = "%%%not-a-grant%%%"
    decision = present_or_reject(_NeverPresenter(), raw, now=0, required=None)
    assert isinstance(decision, AuthDecision)
    assert decision.allowed is False
    assert decision.code == "invalid_signature"
    assert decision.message == "invalid grant"
    assert raw not in (decision.message or "")
