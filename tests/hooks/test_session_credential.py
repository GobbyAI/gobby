"""SessionStart hands its machine caller a token bound to the session it started."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
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
from gobby.hooks.session_claimant import Claimant, claim_session_start, resolve_claimant
from gobby.hooks.session_credential import SESSION_CREDENTIAL_FIELD, mint_session_credential
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminal_context import parse_terminal_context_value
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


HOOK_PID = 5151
CLI_PID = 4242
OTHER_CLI_PID = 5555


@pytest.fixture
def started_session(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> Any:
    session = session_manager.register(
        external_id=PROVIDER_SESSION,
        machine_id=TEST_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps({"parent_pid": CLI_PID}), session.id),
    )
    return session


@pytest.fixture
def cli_claimant() -> Iterator[Claimant]:
    """The machine caller's hook process, whose parent is the session's CLI."""
    claimant = Claimant(pid=HOOK_PID, parent_pid=CLI_PID, cli_pid=CLI_PID)
    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=claimant):
        yield claimant


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
    server: Any


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
            yield HookRoute(client=client, auth_service=auth_service, server=server)
    finally:
        manager.shutdown()


def _session_start_response(
    route: HookRoute,
    *,
    session_id: str = PROVIDER_SESSION,
    start_source: str = "startup",
    terminal_context: dict[str, Any] | None = None,
    cwd: str = "/tmp",
    headers: dict[str, str] | None = None,
) -> Any:
    input_data: dict[str, Any] = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "source": start_source,
        "cwd": cwd,
        "machine_id": TEST_MACHINE_ID,
    }
    if terminal_context is not None:
        input_data["terminal_context"] = terminal_context
    # gdaemon always injects the accepted socket's port; the default stands in for it.
    peer_headers = {"X-Gobby-Peer-Port": "53412"} if headers is None else headers
    return route.client.post(
        "/api/hooks/execute",
        headers=peer_headers,
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "session-start",
            "source": "claude",
            "input_data": input_data,
        },
    )


def _post_session_start(route: HookRoute, **kwargs: Any) -> dict[str, Any]:
    response = _session_start_response(route, **kwargs)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_session_start_from_the_machine_caller_carries_its_session_credential(
    hook_route: HookRoute, started_session: Any, cli_claimant: Claimant
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


def _machine_caller(route: HookRoute) -> None:
    route.auth_service.verified_front_door_identity = MagicMock(
        return_value=SimpleNamespace(machine_id=TEST_MACHINE_ID)
    )


def test_machine_start_naming_a_session_owned_by_another_cli_is_refused(
    hook_route: HookRoute, started_session: Any, temp_db: HubDatabase
) -> None:
    _machine_caller(hook_route)
    other_cli = Claimant(pid=HOOK_PID, parent_pid=OTHER_CLI_PID, cli_pid=OTHER_CLI_PID)

    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=other_cli):
        response = _session_start_response(hook_route, terminal_context={"parent_pid": CLI_PID})

    assert response.status_code == 403
    assert response.json() == {"code": "session_claimant_mismatch"}
    assert SESSION_CREDENTIAL_FIELD not in response.text
    row = temp_db.fetchone(
        "SELECT terminal_context FROM sessions WHERE id = %s", (started_session.id,)
    )
    assert row is not None
    context = parse_terminal_context_value(row["terminal_context"]) or {}
    assert context["parent_pid"] == CLI_PID


def test_machine_start_from_an_unresolvable_claimant_is_refused(
    hook_route: HookRoute, started_session: Any
) -> None:
    _machine_caller(hook_route)

    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=None):
        response = _session_start_response(hook_route)

    assert response.status_code == 403
    assert response.json() == {"code": "claimant_unverified"}


def test_daemon_replayed_start_carries_no_credential(
    hook_route: HookRoute, started_session: Any
) -> None:
    _machine_caller(hook_route)
    daemon = Claimant(pid=os.getpid(), parent_pid=CLI_PID, cli_pid=CLI_PID)

    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=daemon):
        body = _post_session_start(hook_route)

    assert SESSION_CREDENTIAL_FIELD not in body


def test_clear_successor_start_mints_a_new_credential(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    tmp_path: Path,
    started_session: Any,
    cli_claimant: Claimant,
) -> None:
    _machine_caller(hook_route)
    successor = "provider-session-after-clear"

    body = _post_session_start(
        hook_route,
        session_id=successor,
        start_source="clear",
        terminal_context={"parent_pid": CLI_PID},
        cwd=str(tmp_path / "isolated-checkout"),
    )

    credential = body[SESSION_CREDENTIAL_FIELD]
    row = temp_db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s AND session_type = 'terminal'",
        (successor,),
    )
    assert row is not None
    assert credential["session_id"] == str(row["id"])
    assert credential["session_id"] != started_session.id


def test_resolve_claimant_finds_this_process_behind_a_loopback_socket() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        try:
            claimant = resolve_claimant(
                client_port=client.getsockname()[1],
                server_port=listener.getsockname()[1],
            )
            assert claimant is not None
            assert claimant.pid == os.getpid()
        finally:
            accepted.close()
    finally:
        client.close()
        listener.close()


@pytest.mark.parametrize("peer_port", [None, "0", "not-a-port"])
def test_front_door_start_without_a_usable_peer_port_is_unverified(
    hook_route: HookRoute, started_session: Any, peer_port: str | None
) -> None:
    _machine_caller(hook_route)
    headers = {} if peer_port is None else {"X-Gobby-Peer-Port": peer_port}

    response = _session_start_response(hook_route, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"code": "claimant_unverified"}


def test_front_door_start_attests_the_peer_port_gdaemon_observed(
    hook_route: HookRoute, started_session: Any
) -> None:
    # Behind gdaemon the backend's own client port is 0, so the claim reads the port gdaemon
    # observed on its accepted socket. The loopback pair stands in for the hook's socket.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        try:
            hook_route.server.bootstrap_config = BootstrapConfig(
                daemon_port=listener.getsockname()[1]
            )
            _machine_caller(hook_route)
            with patch(
                "gobby.hooks.session_claimant.claim_session_start",
                wraps=claim_session_start,
            ) as claim:
                _session_start_response(
                    hook_route,
                    headers={"X-Gobby-Peer-Port": str(client.getsockname()[1])},
                )
        finally:
            accepted.close()
    finally:
        client.close()
        listener.close()

    claimant = claim.call_args.kwargs["claimant"]
    assert claimant is not None
    assert claimant.pid == os.getpid()
