"""SessionStart hands its machine caller a token bound to the session it started."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.hooks.session_credential import SESSION_CREDENTIAL_FIELD, mint_session_credential
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import (
    SESSION_TOKEN_KIND,
    derive_managed_signing_key,
    verify_agent_api_token,
)
from tests.servers.conftest import authenticate_test_server

pytestmark = pytest.mark.unit

TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
SIGNING_KEY = derive_managed_signing_key("session-credential-machine-key")
PROVIDER_SESSION = "provider-session-start"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", TEST_MACHINE_ID):
        yield


@pytest.fixture
def started_session(session_manager: SessionManager, sample_project: dict[str, Any]) -> Any:
    return session_manager.register(
        external_id=PROVIDER_SESSION,
        machine_id=TEST_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )


def test_mint_hands_back_a_token_bound_to_the_live_session(
    temp_db: HubDatabase, started_session: Any
) -> None:
    credential = mint_session_credential(
        temp_db,
        signing_key=SIGNING_KEY,
        external_id=PROVIDER_SESSION,
        source="claude",
        machine_id=TEST_MACHINE_ID,
    )

    assert credential is not None
    assert credential["session_id"] == started_session.id
    claims = verify_agent_api_token(credential["token"], SIGNING_KEY)
    assert claims is not None
    assert claims.session_id == started_session.id
    assert claims.kind == SESSION_TOKEN_KIND


def test_mint_issues_nothing_for_an_unknown_provider_session(temp_db: HubDatabase) -> None:
    assert (
        mint_session_credential(
            temp_db,
            signing_key=SIGNING_KEY,
            external_id="never-started",
            source="claude",
            machine_id=TEST_MACHINE_ID,
        )
        is None
    )


def test_mint_issues_nothing_once_the_session_has_ended(
    temp_db: HubDatabase, started_session: Any
) -> None:
    temp_db.execute(
        "UPDATE sessions SET status = %s WHERE id = %s", ("expired", started_session.id)
    )

    assert (
        mint_session_credential(
            temp_db,
            signing_key=SIGNING_KEY,
            external_id=PROVIDER_SESSION,
            source="claude",
            machine_id=TEST_MACHINE_ID,
        )
        is None
    )


def test_mint_issues_nothing_without_a_signing_key(
    temp_db: HubDatabase, started_session: Any
) -> None:
    assert (
        mint_session_credential(
            temp_db,
            signing_key=None,
            external_id=PROVIDER_SESSION,
            source="claude",
            machine_id=TEST_MACHINE_ID,
        )
        is None
    )


@dataclass(frozen=True)
class HookRoute:
    client: TestClient
    auth_service: Any


@pytest.fixture
def hook_route(temp_db: HubDatabase) -> Iterator[HookRoute]:
    """The daemon's real hook route over a real HookManager on the test hub."""
    sessions = SessionManager(temp_db)
    server = authenticate_test_server(
        HTTPServer(
            services=ServiceContainer(
                database=temp_db,
                session_manager=sessions,
                task_manager=MagicMock(),
            ),
            port=60888,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
        )
    )
    auth_service = cast(Any, server.auth_service)
    auth_service.managed_signing_key = MagicMock(return_value=SIGNING_KEY)
    manager = HookManager(database=temp_db, session_manager=sessions, config=DaemonConfig())
    server.app.state.hook_manager = manager
    try:
        with TestClient(server.app) as client:
            yield HookRoute(client=client, auth_service=auth_service)
    finally:
        manager.shutdown()


def _post_session_start(route: HookRoute) -> dict[str, Any]:
    response = route.client.post(
        "/api/hooks/execute",
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {
                "hook_event_name": "SessionStart",
                "session_id": PROVIDER_SESSION,
                "source": "startup",
                "cwd": "/tmp",
                "machine_id": TEST_MACHINE_ID,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_session_start_from_the_machine_caller_carries_its_session_credential(
    hook_route: HookRoute, started_session: Any
) -> None:
    hook_route.auth_service.verified_front_door_identity = MagicMock(
        return_value=SimpleNamespace(machine_id=TEST_MACHINE_ID)
    )

    body = _post_session_start(hook_route)

    credential = body[SESSION_CREDENTIAL_FIELD]
    assert credential["session_id"] == started_session.id
    claims = verify_agent_api_token(credential["token"], SIGNING_KEY)
    assert claims is not None
    assert claims.session_id == started_session.id


def test_session_start_without_front_door_identity_carries_no_credential(
    hook_route: HookRoute, started_session: Any
) -> None:
    hook_route.auth_service.verified_front_door_identity = MagicMock(return_value=None)

    body = _post_session_start(hook_route)

    assert SESSION_CREDENTIAL_FIELD not in body
