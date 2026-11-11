"""gmcp under Codex's shared app-server exchanges a thread id for that thread's token (#25905).

Josh's design: a hand-started thread's start registers it to the live shared worker that
runs its hooks, by pid and start time. gmcp, a child of that same worker, presents the
thread id Codex puts in each tool call's ``_meta``. The daemon reads gmcp's parent from
the kernel socket table and binds the call only when that parent is the live worker the
thread registered from, it runs as this daemon's user, and no managed run is presented.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.codex_thread_hosts import ThreadHost, register_thread_host
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import verify_agent_api_token
from gobby.utils.session_context import AGENT_RUN_ID_HEADER
from tests.hooks.test_session_credential import (
    CODEX_GMCP_PID,
    CODEX_OTHER_HOST_PID,
    CODEX_OTHER_THREAD,
    CODEX_PROVIDER_SESSION,
    CODEX_SHARED_HOST_PID,
    CODEX_TUI_PID,
    FAKE_PROCESS_START,
    SIGNING_KEY,
    TEST_MACHINE_ID,
    HookRoute,
    _codex_no_daemon_tree,
    _codex_shared_host_tree,
    _local_machine_identity,
    _machine_caller,
    _process_tree,
    hook_route,
)

pytestmark = pytest.mark.unit

# Fixtures imported for this module's tests.
__all__ = ["_local_machine_identity", "hook_route"]

EXCHANGE = "/api/hooks/codex-thread-credential"
UNBOUND = {"success": False, "error_code": "session_credential_unbound"}
WORKER = ThreadHost(CODEX_SHARED_HOST_PID, FAKE_PROCESS_START)


@contextmanager
def _gmcp_in(tree: Any) -> Iterator[None]:
    """gmcp owns the request's socket; the daemon resolves its parent through `tree`."""
    with (
        patch("gobby.hooks.session_claimant._socket_owner", return_value=CODEX_GMCP_PID),
        patch("gobby.hooks.session_claimant.psutil", tree),
        patch("gobby.hooks.terminal_context.psutil", tree),
    ):
        yield


def _exchange(route: HookRoute, thread: Any, headers: dict[str, str] | None = None) -> Any:
    return route.client.post(
        EXCHANGE,
        headers={"X-Gobby-Peer-Port": "53412", **(headers or {})},
        json={"thread_id": thread},
    )


def _thread_session(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project: dict[str, Any],
    thread: str,
    host: ThreadHost | None = WORKER,
) -> Any:
    session = session_manager.register(
        external_id=thread, machine_id=TEST_MACHINE_ID, source="codex", project_id=project["id"]
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps({"parent_pid": CODEX_TUI_PID}), session.id),
    )
    if host is not None:
        register_thread_host(temp_db, machine_id=TEST_MACHINE_ID, external_id=thread, host=host)
    return session


def _bound_session(response: Any) -> str:
    assert response.status_code == 200, response.text
    body = response.json()
    claims = verify_agent_api_token(body["token"], SIGNING_KEY)
    assert claims is not None
    assert claims.session_id == body["session_id"]
    return str(body["session_id"])


def test_two_threads_on_one_worker_each_bind_to_their_own_session(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    _machine_caller(hook_route)
    first = _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)
    second = _thread_session(temp_db, session_manager, sample_project, CODEX_OTHER_THREAD)

    with _gmcp_in(_codex_shared_host_tree()):
        first_bound = _bound_session(_exchange(hook_route, CODEX_PROVIDER_SESSION))
        second_bound = _bound_session(_exchange(hook_route, CODEX_OTHER_THREAD))

    assert first_bound == first.id
    assert second_bound == second.id


@pytest.mark.parametrize("thread", ["01a1b24d-0000-7000-8000-0000000259ff", "", None, 7])
def test_a_thread_no_start_registered_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    thread: Any,
) -> None:
    _machine_caller(hook_route)
    _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)

    with _gmcp_in(_codex_shared_host_tree()):
        response = _exchange(hook_route, thread)

    assert response.status_code == 403
    assert response.json() == UNBOUND


def test_a_restarted_worker_is_refused_until_its_thread_registers_again(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """The pid survived the restart, but its start time names the worker that died."""
    _machine_caller(hook_route)
    earlier = ThreadHost(CODEX_SHARED_HOST_PID, FAKE_PROCESS_START - 600)
    session = _thread_session(
        temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION, host=earlier
    )

    with _gmcp_in(_codex_shared_host_tree()):
        refused = _exchange(hook_route, CODEX_PROVIDER_SESSION)
        register_thread_host(
            temp_db,
            machine_id=TEST_MACHINE_ID,
            external_id=CODEX_PROVIDER_SESSION,
            host=WORKER,
            displaced=earlier,
        )
        bound = _exchange(hook_route, CODEX_PROVIDER_SESSION)

    assert refused.status_code == 403
    assert refused.json() == UNBOUND
    assert _bound_session(bound) == session.id


def test_a_dead_worker_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """The registered worker is gone; gmcp under a different worker cannot inherit it."""
    _machine_caller(hook_route)
    _thread_session(
        temp_db,
        session_manager,
        sample_project,
        CODEX_PROVIDER_SESSION,
        host=ThreadHost(CODEX_OTHER_HOST_PID, FAKE_PROCESS_START),
    )

    with _gmcp_in(_codex_shared_host_tree()):
        response = _exchange(hook_route, CODEX_PROVIDER_SESSION)

    assert response.status_code == 403
    assert response.json() == UNBOUND


def test_a_parent_that_is_not_a_shared_worker_cannot_use_the_thread_path(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """gmcp under its own --no-daemon TUI binds through its CLI, never by naming a thread."""
    _machine_caller(hook_route)
    _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)

    with _gmcp_in(_codex_no_daemon_tree()):
        response = _exchange(hook_route, CODEX_PROVIDER_SESSION)

    assert response.status_code == 403
    assert response.json() == UNBOUND


def test_a_worker_run_by_another_user_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    _machine_caller(hook_route)
    _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)
    tree = _codex_shared_host_tree()
    tree.Process(CODEX_SHARED_HOST_PID).uid = os.getuid() + 1

    with _gmcp_in(tree):
        response = _exchange(hook_route, CODEX_PROVIDER_SESSION)

    assert response.status_code == 403
    assert response.json() == UNBOUND


@pytest.mark.parametrize(
    "managed", [{AGENT_RUN_ID_HEADER: "run-25905"}, {"X-Gobby-Session-Id": "session-25905"}]
)
def test_a_managed_caller_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    managed: dict[str, str],
) -> None:
    """A managed seat presents its own run token; the thread path is never its fallback."""
    _machine_caller(hook_route)
    _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)

    with _gmcp_in(_codex_shared_host_tree()):
        response = _exchange(hook_route, CODEX_PROVIDER_SESSION, headers=managed)

    assert response.status_code in {401, 403}
    assert "token" not in response.text


def test_a_caller_without_front_door_identity_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    _thread_session(temp_db, session_manager, sample_project, CODEX_PROVIDER_SESSION)
    hook_route.auth_service.verified_front_door_identity = lambda request: None

    with _gmcp_in(_codex_shared_host_tree()):
        response = _exchange(hook_route, CODEX_PROVIDER_SESSION)

    assert response.status_code == 403
    assert response.json() == UNBOUND


def test_a_registered_thread_with_no_row_yet_is_refused(
    hook_route: HookRoute, temp_db: HubDatabase
) -> None:
    """The startup registered the thread and deferred its row; gmcp asks again later."""
    _machine_caller(hook_route)
    register_thread_host(
        temp_db, machine_id=TEST_MACHINE_ID, external_id=CODEX_OTHER_THREAD, host=WORKER
    )
    tree = _process_tree(
        (CODEX_SHARED_HOST_PID, "codex", ["codex", "app-server", "--managed-daemon"], None),
        (CODEX_GMCP_PID, "gmcp", ["gmcp"], CODEX_SHARED_HOST_PID),
    )

    with _gmcp_in(tree):
        response = _exchange(hook_route, CODEX_OTHER_THREAD)

    assert response.status_code == 403
    assert response.json() == UNBOUND
