"""Grant presentation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from gobby.runtime_grants import RequiredCapability
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal
from gobby.servers.grant_auth import AuthDecision, bearer_matches_grant, present_or_reject
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import AgentApiTokenClaims

pytestmark = pytest.mark.unit

MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EXECUTION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

PrincipalKind = Literal["interactive", "agent_run", "tool_chat", "maintenance"]


def _managed_claims(kind: str | None) -> AgentApiTokenClaims:
    return AgentApiTokenClaims(
        session_id=EXECUTION_ID,
        project_id=PROJECT_ID,
        machine_id=MACHINE_ID,
        iat=1_700_000_000,
        exp=1_700_003_600,
        managed_execution_id=EXECUTION_ID,
        kind=kind,
    )


def _principal(kind: PrincipalKind) -> GrantPrincipal:
    return GrantPrincipal(
        kind=kind,
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        execution_id=EXECUTION_ID,
        session_id=None,
    )


@pytest.mark.parametrize(
    ("token_kind", "principal_kind", "expected"),
    [
        ("maintenance", "maintenance", True),
        ("maintenance", "tool_chat", False),
        ("tool_chat", "tool_chat", True),
        ("tool_chat", "maintenance", False),
        (None, "tool_chat", True),
        (None, "maintenance", False),
        ("agent_run", "agent_run", False),
    ],
)
def test_bearer_matches_grant_binds_managed_execution_kind(
    token_kind: str | None, principal_kind: PrincipalKind, expected: bool
) -> None:
    """A managed_execution_id token matches only the grant kind its `kind` claim names."""
    matched = bearer_matches_grant(
        _principal(principal_kind),
        claims=_managed_claims(token_kind),
        local_machine_id=MACHINE_ID,
    )
    assert matched is expected


def test_bearer_matches_grant_agent_run_token_matches_only_agent_run_grant() -> None:
    claims = AgentApiTokenClaims(
        session_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        project_id=PROJECT_ID,
        machine_id=MACHINE_ID,
        iat=1_700_000_000,
        exp=1_700_003_600,
        agent_run_id=EXECUTION_ID,
    )
    assert bearer_matches_grant(_principal("agent_run"), claims=claims, local_machine_id=MACHINE_ID)
    assert not bearer_matches_grant(
        _principal("maintenance"), claims=claims, local_machine_id=MACHINE_ID
    )


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


def test_interactive_grant_matches_forwarded_machine(temp_db: HubDatabase, tmp_path: Path) -> None:
    from gobby.runtime_grants import encode_grant_header
    from tests.servers.test_auth_service import (
        _GRANT_NOW,
        _OPERATOR_IDENTITY,
        _grant_auth_service,
        _grant_service,
        _request,
        _signed_presentation_grant,
    )

    service, headers = _grant_auth_service(temp_db, tmp_path)
    service.bind_runtime(
        grant_service=_grant_service(),
        lease_live=lambda: True,
        local_machine_id="hub-machine",
        effect_fence=None,
        clock=lambda: _GRANT_NOW + 10,
        front_door_secret=_OPERATOR_IDENTITY["X-Gobby-Front-Door"],
    )
    grant = _signed_presentation_grant(machine_id="node-machine")
    headers.update(
        {
            "X-Gobby-Machine-Id": "node-machine",
            "X-Gobby-Runtime-Grant": encode_grant_header(grant),
        }
    )
    admitted = service.authenticate(_request(headers, path="/api/llm/status"))
    assert admitted.allowed is True
    assert admitted.principal is not None
    assert admitted.principal.machine_id == "node-machine"
    assert admitted.machine_id == "node-machine"
    forged = service.authenticate(
        _request(headers | {"X-Gobby-Machine-Id": "hub-machine"}, path="/api/llm/status")
    )
    assert forged.allowed is False
    assert forged.code == "forged_identity"
