"""SessionStart hands its machine caller a token bound to the session it started."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import psutil
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
from gobby.storage.codex_thread_hosts import ThreadHost, register_thread_host, thread_host
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import StartupPin
from gobby.storage.sessions import SessionManager
from gobby.terminal_context import parse_terminal_context_value
from gobby.utils.local_token import (
    SESSION_TOKEN_KIND,
    derive_managed_signing_key,
    verify_agent_api_token,
)
from gobby.utils.session_credential_record import cli_pid_key, load_record, stable_cli_pid
from tests.fixtures.gdaemon_binary import checkout_debug_dir
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
# The session's CLI is a live process, so a claim by another CLI is a refusal, not a takeover.
CLI_PID = os.getppid()
OTHER_CLI_PID = 5555
FAKE_PROCESS_START = 1_794_336_237.0
OTHER_CLI = Claimant(
    pid=HOOK_PID, parent_pid=OTHER_CLI_PID, cli_pid=OTHER_CLI_PID, create_time=FAKE_PROCESS_START
)


def _started(pid: int) -> float:
    """The start time that names live process ``pid``."""
    return float(psutil.Process(pid).create_time())


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
        (
            json.dumps({"parent_pid": CLI_PID, "parent_create_time": _started(CLI_PID)}),
            session.id,
        ),
    )
    return session


@pytest.fixture
def cli_claimant() -> Iterator[Claimant]:
    """The machine caller's hook process, whose parent is the session's CLI."""
    claimant = Claimant(
        pid=HOOK_PID, parent_pid=CLI_PID, cli_pid=CLI_PID, create_time=_started(CLI_PID)
    )
    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=claimant):
        yield claimant


@pytest.fixture
def live_cli_claimant() -> Iterator[Claimant]:
    """A claimant whose CLI is this test process, for a row the hook itself creates.

    A new row stores the CLI pid only after it is verified against the live process
    table, and a mint for that row requires it.
    """
    cli_pid = os.getpid()
    claimant = Claimant(
        pid=HOOK_PID, parent_pid=cli_pid, cli_pid=cli_pid, create_time=_started(cli_pid)
    )
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


def test_mint_for_a_claimed_cli_requires_the_row_to_store_that_cli(
    temp_db: HubDatabase, started_session: Any
) -> None:
    """A claimant whose own row creation lost a race to another CLI gets no token."""
    mint = partial(
        mint_session_credential,
        temp_db,
        signing_key=SIGNING_KEY,
        external_id=PROVIDER_SESSION,
        source="claude",
        machine_id=TEST_MACHINE_ID,
    )

    assert mint(owner=StartupPin(OTHER_CLI_PID, _started(CLI_PID))) is None
    assert mint(owner=StartupPin(CLI_PID, FAKE_PROCESS_START)) is None
    owned = mint(owner=StartupPin(CLI_PID, _started(CLI_PID)))
    assert owned is not None
    assert owned["session_id"] == started_session.id


def test_mint_for_a_claimed_cli_refuses_a_row_that_names_its_cli_by_pid_alone(
    temp_db: HubDatabase, started_session: Any
) -> None:
    """A pid is reused; only the pid with its start time names the CLI that owns the row."""
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps({"parent_pid": CLI_PID}), started_session.id),
    )

    assert (
        mint_session_credential(
            temp_db,
            signing_key=SIGNING_KEY,
            external_id=PROVIDER_SESSION,
            source="claude",
            machine_id=TEST_MACHINE_ID,
            owner=StartupPin(CLI_PID, _started(CLI_PID)),
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
    with daemon_route(temp_db) as route:
        yield route


@contextmanager
def daemon_route(temp_db: HubDatabase) -> Iterator[HookRoute]:
    """One daemon process's hook route; a second one over the same hub is a restart."""
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
    source: str = "claude",
    hook_type: str = "session-start",
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
            "hook_type": hook_type,
            "source": source,
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


@pytest.mark.parametrize("hook_type", ["pre-tool-use", "stop", "session-end"])
@pytest.mark.parametrize("claimant", [None, OTHER_CLI])
def test_machine_non_start_cannot_select_another_clis_body_session(
    hook_route: HookRoute,
    started_session: Any,
    hook_type: str,
    claimant: Claimant | None,
) -> None:
    _machine_caller(hook_route)
    with (
        patch("gobby.hooks.session_claimant.resolve_claimant", return_value=claimant),
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter.handle_native") as handle,
    ):
        response = hook_route.client.post(
            "/api/hooks/execute",
            headers={"X-Gobby-Peer-Port": "53412"},
            json={
                "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
                "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
                "critical": False,
                "hook_type": hook_type,
                "source": "claude",
                "input_data": {
                    "session_id": started_session.external_id,
                    "machine_id": TEST_MACHINE_ID,
                    "terminal_context": {"parent_pid": CLI_PID},
                },
            },
        )
    assert response.status_code == 403
    assert response.json() == {
        "code": "claimant_unverified" if claimant is None else "session_claimant_mismatch"
    }
    handle.assert_not_called()


def test_machine_start_naming_a_session_owned_by_another_cli_is_refused(
    hook_route: HookRoute, started_session: Any, temp_db: HubDatabase
) -> None:
    _machine_caller(hook_route)
    other_cli = OTHER_CLI

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
    live_cli_claimant: Claimant,
) -> None:
    _machine_caller(hook_route)
    successor = "provider-session-after-clear"

    body = _post_session_start(
        hook_route,
        session_id=successor,
        start_source="clear",
        terminal_context={"parent_pid": live_cli_claimant.cli_pid},
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


# Droid's TUI and exec backend are live processes: the hook enriches each parent pid with its
# create time, and an unverifiable pid is dropped with no identity. The test process stands in
# for the TUI, and its parent for the exec backend.
DROID_TUI_PID = os.getpid()
DROID_BACKEND_PID = os.getppid()
DROID_NEXT_BACKEND_PID = 6262
DROID_PROVIDER_SESSION = "provider-session-droid"


def _droid_claimant(*, backend_pid: int, cli_pid: int) -> Claimant:
    # Droid runs each hook through an exec backend whose parent is the TUI. The hook's
    # parent is the backend, and the stabilized CLI pid is the TUI.
    started = _started(cli_pid) if cli_pid == DROID_TUI_PID else FAKE_PROCESS_START
    return Claimant(pid=HOOK_PID, parent_pid=backend_pid, cli_pid=cli_pid, create_time=started)


def _stored_parent_pid(temp_db: HubDatabase, external_id: str) -> tuple[Any, int | None]:
    row = temp_db.fetchone(
        "SELECT id, terminal_context FROM sessions "
        "WHERE external_id = %s AND session_type = 'terminal'",
        (external_id,),
    )
    assert row is not None
    context = parse_terminal_context_value(row["terminal_context"]) or {}
    return row["id"], context.get("parent_pid")


def test_droid_second_start_from_the_same_tui_is_attested(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    _machine_caller(hook_route)
    cwd = str(tmp_path / "isolated-checkout")

    with patch(
        "gobby.hooks.session_claimant.resolve_claimant",
        return_value=_droid_claimant(backend_pid=DROID_BACKEND_PID, cli_pid=DROID_TUI_PID),
    ):
        first = _post_session_start(
            hook_route,
            session_id=DROID_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": DROID_BACKEND_PID},
            cwd=cwd,
        )

    with patch(
        "gobby.hooks.session_claimant.resolve_claimant",
        return_value=_droid_claimant(backend_pid=DROID_NEXT_BACKEND_PID, cli_pid=DROID_TUI_PID),
    ):
        # The body names the stabilized TUI, as enrichment stores it for a Droid exec backend.
        second = _post_session_start(
            hook_route,
            session_id=DROID_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": DROID_TUI_PID},
            cwd=cwd,
        )

    session_id, stored_parent = _stored_parent_pid(temp_db, DROID_PROVIDER_SESSION)
    assert first[SESSION_CREDENTIAL_FIELD]["session_id"] == str(session_id)
    assert second[SESSION_CREDENTIAL_FIELD]["session_id"] == str(session_id)
    # The session stores the TUI the live check compares, not the Droid exec backend.
    assert stored_parent == DROID_TUI_PID


def test_droid_start_from_a_different_tui_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    _machine_caller(hook_route)
    cwd = str(tmp_path / "isolated-checkout")

    with patch(
        "gobby.hooks.session_claimant.resolve_claimant",
        return_value=_droid_claimant(backend_pid=DROID_BACKEND_PID, cli_pid=DROID_TUI_PID),
    ):
        _post_session_start(
            hook_route,
            session_id=DROID_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": DROID_BACKEND_PID},
            cwd=cwd,
        )

    with patch(
        "gobby.hooks.session_claimant.resolve_claimant",
        return_value=_droid_claimant(backend_pid=DROID_NEXT_BACKEND_PID, cli_pid=OTHER_CLI_PID),
    ):
        response = _session_start_response(
            hook_route,
            session_id=DROID_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": DROID_NEXT_BACKEND_PID},
            cwd=cwd,
        )

    assert response.status_code == 403
    assert response.json() == {"code": "session_claimant_mismatch"}
    assert SESSION_CREDENTIAL_FIELD not in response.text
    _, stored_parent = _stored_parent_pid(temp_db, DROID_PROVIDER_SESSION)
    assert stored_parent == DROID_TUI_PID


# Codex-shaped process trees. The pids sit above any real pid range, so nothing outside
# the synthetic tree can match them.
CODEX_PROVIDER_SESSION = "01a1b24d-0000-7000-8000-000000025904"
CODEX_HOOK_PID = 4_000_001
CODEX_OLD_TUI_PID = 4_000_002
CODEX_TUI_PID = 4_000_003
CODEX_SHARED_HOST_PID = 4_000_004
CODEX_GMCP_PID = 4_000_005
CODEX_OTHER_HOST_PID = 4_000_006
CODEX_OTHER_THREAD = "01a1b24d-0000-7000-8000-000000025906"


@dataclass
class _FakeProcess:
    """A psutil.Process stand-in for one node of a synthetic process tree."""

    pid: int
    proc_name: str
    argv: list[str]
    parent_pid: int | None
    table: dict[int, _FakeProcess]
    started: float = FAKE_PROCESS_START
    uid: int = os.getuid()

    def name(self) -> str:
        return self.proc_name

    def cmdline(self) -> list[str]:
        return list(self.argv)

    def parent(self) -> _FakeProcess | None:
        return None if self.parent_pid is None else self.table[self.parent_pid]

    def create_time(self) -> float:
        return self.started

    def uids(self) -> SimpleNamespace:
        return SimpleNamespace(real=self.uid, effective=self.uid, saved=self.uid)

    def environ(self) -> dict[str, str]:
        # An operator-launched seat carries no Gobby launch variables.
        return {}

    def terminal(self) -> str | None:
        return "/dev/ttys999" if self.proc_name == "codex" else None

    @property
    def info(self) -> dict[str, Any]:
        """What psutil.process_iter(attrs=[...]) attaches to each process it yields."""
        return {"pid": self.pid, "name": self.proc_name, "cmdline": list(self.argv)}


def _process_tree(*nodes: tuple[int, str, list[str], int | None]) -> Any:
    """A psutil stand-in whose Process() and process_iter() see only `nodes`."""
    table: dict[int, _FakeProcess] = {}
    for pid, name, argv, parent_pid in nodes:
        table[pid] = _FakeProcess(pid, name, argv, parent_pid, table)

    def process(pid: int) -> _FakeProcess:
        try:
            return table[pid]
        except KeyError:
            raise psutil.NoSuchProcess(pid) from None

    return SimpleNamespace(
        Process=process,
        Error=psutil.Error,
        process_iter=lambda *args, **kwargs: iter(list(table.values())),
    )


def _codex_shared_host_tree() -> Any:
    """Codex's shared app-server daemon runs the seat's hooks and its gmcp."""
    return _process_tree(
        (CODEX_SHARED_HOST_PID, "codex", ["codex", "app-server", "--managed-daemon"], None),
        (CODEX_TUI_PID, "codex", ["codex", "resume", CODEX_PROVIDER_SESSION], None),
        (CODEX_HOOK_PID, "ghook", ["ghook", "--cli=codex"], CODEX_SHARED_HOST_PID),
        (CODEX_GMCP_PID, "gmcp", ["gmcp"], CODEX_SHARED_HOST_PID),
    )


def _codex_no_daemon_tree() -> Any:
    """A `--no-daemon` TUI runs its own server, so it parents its hooks and its gmcp."""
    return _process_tree(
        (
            CODEX_TUI_PID,
            "codex",
            ["codex", "resume", "--no-daemon", CODEX_PROVIDER_SESSION],
            None,
        ),
        (CODEX_HOOK_PID, "ghook", ["ghook", "--cli=codex"], CODEX_TUI_PID),
        (CODEX_GMCP_PID, "gmcp", ["gmcp"], CODEX_TUI_PID),
    )


@pytest.fixture
def expired_codex_session(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> Any:
    """An unspawned Codex seat whose old TUI died and was expired by the liveness monitor."""
    session = session_manager.register(
        external_id=CODEX_PROVIDER_SESSION,
        machine_id=TEST_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb, status = 'expired' WHERE id = %s",
        (
            json.dumps({"parent_pid": CODEX_OLD_TUI_PID, "parent_create_time": FAKE_PROCESS_START}),
            session.id,
        ),
    )
    return session


@contextmanager
def _hook_socket_in(tree: Any) -> Iterator[None]:
    """Run the real resolve_claimant against `tree`; only the kernel socket lookup is fixed."""
    with (
        patch("gobby.hooks.session_claimant._socket_owner", return_value=CODEX_HOOK_PID),
        patch("gobby.hooks.session_claimant.psutil", tree),
        patch("gobby.hooks.terminal_context.psutil", tree),
    ):
        yield


def _gmcp_lookup_pid(tree: Any) -> int | None:
    """The CLI pid the gmcp in `tree` reads its session-bound credential under."""
    gmcp_parent = tree.Process(CODEX_GMCP_PID).parent_pid
    with (
        patch("gobby.utils.session_credential_record.psutil", tree),
        patch("gobby.utils.session_credential_record.os.getppid", return_value=gmcp_parent),
    ):
        return stable_cli_pid()


SHARED_WORKER = ThreadHost(CODEX_SHARED_HOST_PID, FAKE_PROCESS_START)


def _registered_worker(temp_db: HubDatabase, thread: str = CODEX_PROVIDER_SESSION) -> Any:
    return thread_host(temp_db, machine_id=TEST_MACHINE_ID, external_id=thread)


def test_a_shared_host_resume_registers_its_thread_to_the_worker_and_mints(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    expired_codex_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Josh's design: the thread is registered to its live worker when it is assigned its id."""
    _machine_caller(hook_route)
    tree = _codex_shared_host_tree()

    with _hook_socket_in(tree), caplog.at_level("WARNING"):
        body = _post_session_start(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": CODEX_SHARED_HOST_PID},
        )

    # The synthetic tree is complete enough that the SessionStart handler itself ran cleanly.
    assert not [m for m in caplog.messages if "Event handler" in m]
    assert _registered_worker(temp_db) == SHARED_WORKER
    assert body[SESSION_CREDENTIAL_FIELD]["session_id"] == expired_codex_session.id


def test_a_client_under_the_shared_worker_reads_no_cli_record() -> None:
    """The worker's pid names no thread, so its record is never read (gmcp, stdio proxy)."""
    assert _gmcp_lookup_pid(_codex_shared_host_tree()) is None
    assert _gmcp_lookup_pid(_codex_no_daemon_tree()) == CODEX_TUI_PID


def test_a_thread_registered_to_another_live_worker_is_refused(
    hook_route: HookRoute, temp_db: HubDatabase, expired_codex_session: Any
) -> None:
    _machine_caller(hook_route)
    other = ThreadHost(CODEX_OTHER_HOST_PID, FAKE_PROCESS_START)
    register_thread_host(
        temp_db, machine_id=TEST_MACHINE_ID, external_id=CODEX_PROVIDER_SESSION, host=other
    )
    tree = _process_tree(
        (CODEX_SHARED_HOST_PID, "codex", ["codex", "app-server", "--managed-daemon"], None),
        (CODEX_OTHER_HOST_PID, "codex", ["codex", "app-server", "--managed-daemon"], None),
        (CODEX_HOOK_PID, "ghook", ["ghook", "--cli=codex"], CODEX_SHARED_HOST_PID),
    )

    with _hook_socket_in(tree):
        response = _session_start_response(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
        )

    assert response.status_code == 403
    assert response.json() == {"code": "session_claimant_mismatch"}
    assert _registered_worker(temp_db) == other


def test_a_restarted_worker_re_registers_only_on_a_start(
    hook_route: HookRoute, temp_db: HubDatabase, expired_codex_session: Any
) -> None:
    """The pid survived but its start time moved: the registered worker is dead."""
    _machine_caller(hook_route)
    earlier = ThreadHost(CODEX_SHARED_HOST_PID, FAKE_PROCESS_START - 600)
    register_thread_host(
        temp_db, machine_id=TEST_MACHINE_ID, external_id=CODEX_PROVIDER_SESSION, host=earlier
    )
    identity = {"session_id": CODEX_PROVIDER_SESSION, "machine_id": TEST_MACHINE_ID}

    with _hook_socket_in(_codex_shared_host_tree()):
        activity = _hook_response(
            hook_route,
            hook_type="UserPromptSubmit",
            source="codex",
            input_data={**identity, "hook_event_name": "UserPromptSubmit", "prompt": "go"},
        )
        registered_after_activity = _registered_worker(temp_db)
        start = _post_session_start(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
        )

    assert activity.status_code == 200, activity.text
    assert SESSION_CREDENTIAL_FIELD not in activity.text
    assert registered_after_activity == earlier
    assert _registered_worker(temp_db) == SHARED_WORKER
    assert SESSION_CREDENTIAL_FIELD in start


def test_a_shared_host_startup_registers_a_new_thread_before_its_row_exists(
    hook_route: HookRoute, temp_db: HubDatabase
) -> None:
    _machine_caller(hook_route)

    with _hook_socket_in(_codex_shared_host_tree()):
        body = _post_session_start(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_OTHER_THREAD,
            start_source="startup",
        )

    # The row is deferred to the first prompt, so there is no session to mint for yet;
    # gmcp exchanges the thread for its token once the row exists.
    assert SESSION_CREDENTIAL_FIELD not in body
    assert _registered_worker(temp_db, CODEX_OTHER_THREAD) == SHARED_WORKER


def test_a_shared_host_owned_by_another_user_proves_nothing(
    hook_route: HookRoute, temp_db: HubDatabase, expired_codex_session: Any
) -> None:
    _machine_caller(hook_route)
    tree = _codex_shared_host_tree()
    tree.Process(CODEX_SHARED_HOST_PID).uid = os.getuid() + 1

    with _hook_socket_in(tree):
        response = _session_start_response(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
        )

    assert SESSION_CREDENTIAL_FIELD not in response.text
    assert _registered_worker(temp_db) is None


def _hook_response(
    route: HookRoute, *, hook_type: str, source: str, input_data: dict[str, Any]
) -> Any:
    return route.client.post(
        "/api/hooks/execute",
        headers={"X-Gobby-Peer-Port": "53412"},
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": hook_type,
            "source": source,
            "input_data": input_data,
        },
    )


def test_fresh_startup_receives_its_credential_on_the_hook_that_materializes_it(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    live_cli_claimant: Claimant,
) -> None:
    """A fresh seat's startup defers its row, so the first activity hook mints its token.

    No resume is needed: the activity hook's kernel-attested CLI materializes the row and
    receives the credential in the same response. A later hook from a CLI with no record is
    still attested against the stored CLI pid before anything else is minted.
    """
    _machine_caller(hook_route)
    workspace = tmp_path / "workspace"
    (workspace / ".gobby").mkdir(parents=True)
    (workspace / ".gobby" / "project.json").write_text(json.dumps({"id": sample_project["id"]}))
    external_id = "provider-session-fresh-startup"
    identity = {"session_id": external_id, "cwd": str(workspace), "machine_id": TEST_MACHINE_ID}

    start = _hook_response(
        hook_route,
        hook_type="session-start",
        source="claude",
        input_data={**identity, "hook_event_name": "SessionStart", "source": "startup"},
    )
    deferred = temp_db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s AND source = 'claude'",
        (external_id,),
    )
    activity = _hook_response(
        hook_route,
        hook_type="user-prompt-submit",
        source="claude",
        input_data={**identity, "hook_event_name": "UserPromptSubmit", "prompt": "first"},
    )

    assert start.status_code == 200, start.text
    assert deferred is None, "the fresh startup defers its row to the first activity"
    assert SESSION_CREDENTIAL_FIELD not in start.json()
    assert activity.status_code == 200, activity.text
    row = temp_db.fetchone(
        "SELECT id, terminal_context FROM sessions WHERE external_id = %s AND source = 'claude'",
        (external_id,),
    )
    assert row is not None
    context = parse_terminal_context_value(row["terminal_context"]) or {}
    assert context["parent_pid"] == live_cli_claimant.cli_pid
    credential = activity.json()[SESSION_CREDENTIAL_FIELD]
    assert credential["session_id"] == row["id"]
    claims = verify_agent_api_token(credential["token"], SIGNING_KEY)
    assert claims is not None
    assert claims.session_id == row["id"]

    other_cli = OTHER_CLI
    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=other_cli):
        foreign = _hook_response(
            hook_route,
            hook_type="user-prompt-submit",
            source="claude",
            input_data={**identity, "hook_event_name": "UserPromptSubmit", "prompt": "x"},
        )
    assert foreign.status_code == 403
    assert SESSION_CREDENTIAL_FIELD not in foreign.text


def _fresh_workspace(tmp_path: Path, project: dict[str, Any]) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".gobby").mkdir(parents=True)
    (workspace / ".gobby" / "project.json").write_text(json.dumps({"id": project["id"]}))
    return workspace


def _activity(route: HookRoute, identity: dict[str, Any]) -> Any:
    return _hook_response(
        route,
        hook_type="user-prompt-submit",
        source="claude",
        input_data={**identity, "hook_event_name": "UserPromptSubmit", "prompt": "go"},
    )


def test_a_foreign_cli_cannot_create_a_deferred_startups_row(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    live_cli_claimant: Claimant,
) -> None:
    """The deferred startup pins its proven CLI; another CLI's first hook is refused.

    A process that learns the thread id after launch cannot create the row, so it can
    neither take the session's token nor lock the real CLI out of it.
    """
    _machine_caller(hook_route)
    workspace = _fresh_workspace(tmp_path, sample_project)
    external_id = "provider-session-pinned-startup"
    identity = {"session_id": external_id, "cwd": str(workspace), "machine_id": TEST_MACHINE_ID}
    _hook_response(
        hook_route,
        hook_type="session-start",
        source="claude",
        input_data={**identity, "hook_event_name": "SessionStart", "source": "startup"},
    )

    other_cli = OTHER_CLI
    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=other_cli):
        foreign = _activity(hook_route, identity)

    assert foreign.status_code == 403
    assert foreign.json() == {"code": "session_claimant_mismatch"}
    assert SESSION_CREDENTIAL_FIELD not in foreign.text
    row_query = "SELECT id FROM sessions WHERE external_id = %s AND source = 'claude'"
    assert temp_db.fetchone(row_query, (external_id,)) is None

    own = _activity(hook_route, identity)

    assert own.status_code == 200, own.text
    row = temp_db.fetchone(row_query, (external_id,))
    assert row is not None
    assert own.json()[SESSION_CREDENTIAL_FIELD]["session_id"] == row["id"]


def test_an_attested_activity_hook_on_a_live_session_mints_nothing(
    hook_route: HookRoute, started_session: Any, cli_claimant: Claimant
) -> None:
    """Only a start or the hook that materializes the row mints; ordinary activity does not."""
    _machine_caller(hook_route)

    response = _hook_response(
        hook_route,
        hook_type="user-prompt-submit",
        source="claude",
        input_data={
            "session_id": PROVIDER_SESSION,
            "cwd": "/tmp",
            "machine_id": TEST_MACHINE_ID,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "later prompt",
        },
    )

    assert response.status_code == 200, response.text
    assert SESSION_CREDENTIAL_FIELD not in response.json()


def test_codex_start_from_an_uninspectable_hook_parent_is_not_called_a_shared_host(
    hook_route: HookRoute,
    expired_codex_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _machine_caller(hook_route)
    # The hook's parent has already exited, so the claimant has no attestable CLI.
    tree = _process_tree((CODEX_HOOK_PID, "ghook", ["ghook", "--cli=codex"], None))

    with (
        _hook_socket_in(tree),
        caplog.at_level("WARNING", logger="gobby.hooks.session_claimant"),
    ):
        body = _post_session_start(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
        )

    assert SESSION_CREDENTIAL_FIELD not in body
    warnings = [
        record.getMessage()
        for record in caplog.records
        if CODEX_PROVIDER_SESSION in record.getMessage()
    ]
    assert warnings
    assert all("shared host" not in message for message in warnings), warnings


def test_codex_no_daemon_resume_revives_the_expired_seat_with_a_credential(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    expired_codex_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _machine_caller(hook_route)
    tree = _codex_no_daemon_tree()

    with _hook_socket_in(tree), caplog.at_level("WARNING"):
        body = _post_session_start(
            hook_route,
            source="codex",
            hook_type="SessionStart",
            session_id=CODEX_PROVIDER_SESSION,
            start_source="resume",
            terminal_context={"parent_pid": CODEX_TUI_PID},
        )

    handler_errors = [m for m in caplog.messages if "Event handler" in m]
    assert not handler_errors, handler_errors
    credential = body[SESSION_CREDENTIAL_FIELD]
    assert credential["session_id"] == expired_codex_session.id
    row = temp_db.fetchone(
        "SELECT status, terminal_context FROM sessions WHERE id = %s",
        (expired_codex_session.id,),
    )
    assert row is not None
    assert row["status"] == "active"
    # The claim proved and stored the TUI's pid, the key ghook keeps the credential under,
    # and gmcp, also a child of the TUI, looks it up under that same pid.
    context = parse_terminal_context_value(row["terminal_context"]) or {}
    assert context["parent_pid"] == CODEX_TUI_PID
    assert _gmcp_lookup_pid(tree) == CODEX_TUI_PID


def _checkout_binary(name: str) -> Path:
    """The checkout's debug build of a Rust binary (`cargo build -p ... --bins`)."""
    repo_root = Path(__file__).resolve().parents[2]
    binary = checkout_debug_dir(repo_root, os.environ) / name
    if not binary.is_file():
        pytest.skip(f"{binary} is not built: cargo build -p gobby-hooks -p gobby-mcp --bins")
    return binary


def _client_environment(gobby_home: Path, home: Path, daemon_url: str) -> dict[str, str]:
    """An unspawned CLI's environment: no Gobby launch, run or managed-execution markers."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GOBBY_", "TMUX", "CLAUDE"))
    }
    environment.update(
        {"HOME": str(home), "GOBBY_HOME": str(gobby_home), "GOBBY_DAEMON_URL": daemon_url}
    )
    return environment


class _FrontDoor:
    """Stands in for gdaemon's front door on a real loopback socket.

    A hook POST goes to the real daemon hook route with the peer port gdaemon would add.
    Any other request, as gmcp makes, is recorded with its credential headers.
    """

    def __init__(self, route: HookRoute) -> None:
        self.route = route
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        front_door = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                headers = {key.lower(): value for key, value in self.headers.items()}
                front_door.requests.append((self.command, self.path, headers))
                if self.path == "/api/hooks/execute":
                    forwarded = {
                        "Content-Type": "application/json",
                        "X-Gobby-Peer-Port": "53412",
                    }
                    if "x-gobby-envelope-id" in headers:
                        forwarded["X-Gobby-Envelope-Id"] = headers["x-gobby-envelope-id"]
                    response = front_door.route.client.post(
                        self.path, content=payload, headers=forwarded
                    )
                    self._reply(response.status_code, response.content)
                elif self.path == "/api/health":
                    self._reply(200, b'{"status":"healthy"}')
                elif self.path == "/api/mcp/bridge/tool-timeouts":
                    self._reply(200, b'{"success":true,"tool_timeouts":{}}')
                else:
                    self._reply(200, b'{"success":true,"result":{"reached":true}}')

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _FrontDoor:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _gmcp_call(gmcp: Path, environment: dict[str, str], cwd: Path) -> dict[str, Any]:
    """Make one MCP tool call through the real gmcp binary and return its JSON-RPC reply."""
    process = subprocess.Popen(
        [str(gmcp)],
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    replies: queue.Queue[dict[str, Any]] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                replies.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    threading.Thread(target=read, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def reply(request_id: int) -> dict[str, Any]:
        while True:
            message = replies.get(timeout=20)
            if message.get("id") == request_id:
                return message

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        reply(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "call_tool",
                    "arguments": {
                        "server_name": "example",
                        "tool_name": "echo",
                        "arguments": {"value": "bound"},
                        "preflight_enabled": False,
                    },
                },
            }
        )
        return reply(2)
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.integration
def test_codex_no_daemon_resume_credential_reaches_ghook_store_and_gmcp_lookup(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    expired_codex_session: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """End to end on real binaries: the daemon mints, ghook stores, gmcp finds and sends.

    This test process plays the `codex resume --no-daemon` TUI: it is the parent of the
    real ghook and the real gmcp, as a no-daemon TUI is of both. The synthetic tree only
    describes these real pids to the daemon's claimant check.
    """
    ghook = _checkout_binary("ghook")
    gmcp = _checkout_binary("gmcp")
    _machine_caller(hook_route)
    tui_pid = os.getpid()
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    (gobby_home / "machine_id").write_text(TEST_MACHINE_ID + "\n")
    (gobby_home / "bootstrap.yaml").write_text("api_key: isolated-front-door-key\n")
    # The seat runs in its project's checkout, as an unspawned seat does.
    workspace = tmp_path / "workspace"
    (workspace / ".gobby").mkdir(parents=True)
    (workspace / ".gobby" / "project.json").write_text(json.dumps({"id": sample_project["id"]}))
    hook_started = threading.Event()
    hook_pid: dict[str, int] = {}

    def socket_owner(**_: Any) -> int:
        assert hook_started.wait(timeout=20)
        return hook_pid["pid"]

    with _FrontDoor(hook_route) as front_door:
        environment = _client_environment(gobby_home, tmp_path, front_door.url)
        hook = subprocess.Popen(
            [str(ghook), "--gobby-owned", "--cli=codex", "--type=SessionStart"],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tree = _process_tree(
            (tui_pid, "codex", ["codex", "resume", "--no-daemon", CODEX_PROVIDER_SESSION], None),
            (hook.pid, "ghook", ["ghook", "--cli=codex"], tui_pid),
        )
        with (
            patch("gobby.hooks.session_claimant._socket_owner", side_effect=socket_owner),
            patch("gobby.hooks.session_claimant.psutil", tree),
            patch("gobby.hooks.terminal_context.psutil", tree),
        ):
            hook_pid["pid"] = hook.pid
            hook_started.set()
            stdout, stderr = hook.communicate(
                json.dumps(
                    {
                        "session_id": CODEX_PROVIDER_SESSION,
                        "hook_event_name": "SessionStart",
                        "source": "resume",
                        "cwd": str(workspace),
                    }
                ).encode(),
                timeout=60,
            )

        assert hook.returncode == 0, (stdout, stderr)
        # The credential never reaches the CLI: ghook strips it from what it prints.
        assert SESSION_CREDENTIAL_FIELD.encode() not in stdout
        row = temp_db.fetchone(
            "SELECT status, terminal_context FROM sessions WHERE id = %s",
            (expired_codex_session.id,),
        )
        assert row is not None
        assert row["status"] == "active", (stdout, stderr, front_door.requests)
        context = parse_terminal_context_value(row["terminal_context"]) or {}
        assert context["parent_pid"] == tui_pid

        # ghook's real store kept it under this TUI's pid, the key gmcp reads.
        stored = load_record(gobby_home, cli_pid_key(tui_pid))
        assert stored is not None
        assert stored.session_id == expired_codex_session.id
        claims = verify_agent_api_token(stored.token, SIGNING_KEY)
        assert claims is not None
        assert claims.session_id == expired_codex_session.id

        # gmcp's real lookup finds it and presents it, not the machine key.
        reply = _gmcp_call(gmcp, environment, workspace)
        assert reply.get("error") is None, reply
        tool_calls = [
            headers
            for _, path, headers in front_door.requests
            if path.startswith("/api/mcp/") and path != "/api/mcp/bridge/tool-timeouts"
        ]
        assert tool_calls, front_door.requests
        assert tool_calls[-1]["authorization"] == f"Bearer {stored.token}"
        assert tool_calls[-1]["x-gobby-session-id"] == expired_codex_session.id


@pytest.mark.parametrize(
    ("source", "hook_type"),
    [
        ("claude", "session-start"),
        ("codex", "SessionStart"),
        ("grok", "SessionStart"),
        ("grok", "session_start"),
        ("droid", "SessionStart"),
    ],
)
def test_each_providers_own_session_start_spelling_mints_a_credential(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    source: str,
    hook_type: str,
) -> None:
    """Every provider names SessionStart its own way; each one is a start that mints."""
    _machine_caller(hook_route)
    provider_session = f"provider-session-{source}-{hook_type}"
    session = session_manager.register(
        external_id=provider_session,
        machine_id=TEST_MACHINE_ID,
        source=source,
        project_id=sample_project["id"],
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (
            json.dumps({"parent_pid": CODEX_TUI_PID, "parent_create_time": FAKE_PROCESS_START}),
            session.id,
        ),
    )
    tree = _process_tree(
        (CODEX_TUI_PID, source, [source], None),
        (CODEX_HOOK_PID, "ghook", ["ghook", f"--cli={source}"], CODEX_TUI_PID),
    )

    with _hook_socket_in(tree):
        body = _post_session_start(
            hook_route,
            source=source,
            hook_type=hook_type,
            session_id=provider_session,
            terminal_context={"parent_pid": CODEX_TUI_PID},
        )

    assert body[SESSION_CREDENTIAL_FIELD]["session_id"] == session.id
