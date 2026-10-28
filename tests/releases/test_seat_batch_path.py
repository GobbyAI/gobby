"""A release seat runs the batch without opening daemon credentials.

The seat reaches release storage only through the daemon's release tools. Those
tools run here on the test hub behind the seat's delegated-session seam, so every
call crosses the same token, generation and role checks the daemon route applies.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from click.testing import CliRunner, Result

from gobby.cli.release import release
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry, normalize_internal_success_result
from gobby.mcp_proxy.tools.release_tools import register_release_tools
from gobby.releases.window import ReleaseWindow, acquire_window
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id, session_context_for_test
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.release_batch_case import Clock, Fakes
from tests.fixtures.review_approvals import grant_step_writes

pytestmark = pytest.mark.integration

BATCH_TOOLS = ("release_batch_checkout", "release_batch_active_runs", "release_batch_fenced_runs")
PAYLOAD_KEYS = {
    "release_batch_checkout": {"root", "candidate_sha"},
    "release_batch_active_runs": {"run_ids"},
    "release_batch_fenced_runs": {"run_ids"},
}
DaemonSeam = Callable[[str, str, str, dict[str, object]], Awaitable[tuple[int, object]]]


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


def _daemon_seam(case: LandingCase) -> DaemonSeam:
    """Answer the seat's daemon calls with the gobby-agents release tools on the test hub.

    The delegated session is the caller, as the daemon route takes it from the seat's
    X-Gobby-Session-Id header, so the role gate and the lease fence both apply.
    """
    registry = InternalToolRegistry(name="test-agents", description="test")
    register_release_tools(
        registry,
        AgentsRegistryContext(
            runner=MagicMock(),
            agent_run_manager=LocalAgentRunManager(case.db),
            resolve_session_id=lambda ref: ref,
            get_current_session_id=get_current_session_id,
            get_current_agent_run_id=lambda: None,
            get_project_context=lambda: None,
            db=case.db,
        ),
    )

    async def call(
        session_id: str, server: str, tool: str, arguments: dict[str, object]
    ) -> tuple[int, object]:
        assert server == "gobby-agents", f"the seat reached {server}.{tool} directly"
        with session_context_for_test(session_id):
            payload = normalize_internal_success_result(await registry.call(tool, arguments))
        # The daemon route strips the success marker from a tool result and wraps it under
        # "result"; a refusal keeps success=False and error_code at the top level.
        if isinstance(payload, dict) and payload.get("success") is False:
            return 200, payload
        return 200, {"success": True, "result": payload}

    return call


def _arguments(tool: str, token: str, generation: int) -> dict[str, object]:
    arguments: dict[str, object] = {"token": token, "generation": generation}
    if tool == "release_batch_fenced_runs":
        arguments["run_ids"] = [str(uuid4())]
    return arguments


def _seat_batch(
    monkeypatch: pytest.MonkeyPatch,
    case: LandingCase,
    owner: str,
    token: str,
    generation: int,
    base: str,
) -> Result:
    monkeypatch.setattr("gobby.cli.release._lease_call", _daemon_seam(case))
    return CliRunner().invoke(
        release,
        ["batch", "--token", token, "--generation", str(generation), "--base-sha", base],
        env={"GOBBY_SESSION_ID": owner, "DATABASE_URL": None},
    )


@pytest.mark.parametrize("invalid", [None, "foreign-token", "wrong-generation"])
def test_release_seat_batch_needs_no_local_database_credentials(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch, invalid: str | None
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    candidate = case.direct({"src/gobby/tasks/a.py": "A = 1\n"})
    clock = Clock(datetime.now(UTC))
    lease = asyncio.run(
        acquire_window(case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now)
    )
    fakes = Fakes(case, clock)
    monkeypatch.chdir(case.repo)
    monkeypatch.setattr("gobby.cli.release.get_project_context", lambda: {"id": case.project_id})
    # Daemon-side check/activation ports are isolated; seat-side DB access is denied.
    monkeypatch.setattr("gobby.cli.release._environment", lambda *_args: fakes.env())
    denied = Mock(side_effect=PermissionError("seat cannot read bootstrap.yaml"))
    monkeypatch.setattr("gobby.cli.utils.init_local_storage", denied)
    monkeypatch.setattr("gobby.cli.utils_config.init_local_storage", denied)

    result = _seat_batch(
        monkeypatch,
        case,
        owner,
        "foreign" if invalid == "foreign-token" else lease.token,
        lease.generation + (invalid == "wrong-generation"),
        case.base,
    )

    assert denied.call_count == 0, "public batch path tried to open daemon bootstrap/DB credentials"
    assert "database_url" not in result.output.lower()
    assert "postgresql://" not in result.output
    if invalid is None:
        assert result.exit_code == 0, (result.output, result.exception)
        assert candidate in result.output
        assert '"succeeded"' in result.output
    else:
        assert result.exit_code != 0
        assert "release_window_stale" in result.output
        assert not fakes.checks


@pytest.mark.parametrize("tool", BATCH_TOOLS)
@pytest.mark.parametrize("invalid", ["foreign-token", "wrong-generation"])
async def test_batch_storage_tools_refuse_a_stale_or_foreign_lease(
    case: LandingCase, tool: str, invalid: str
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    token = "foreign" if invalid == "foreign-token" else lease.token
    generation = lease.generation + (invalid == "wrong-generation")

    status, refused = await _daemon_seam(case)(
        owner, "gobby-agents", tool, _arguments(tool, token, generation)
    )

    assert status == 200
    assert isinstance(refused, dict)
    assert (refused["success"], refused["error_code"]) == (False, "release_window_stale"), refused


@pytest.mark.parametrize("tool", BATCH_TOOLS)
async def test_batch_storage_tools_admit_only_the_release_manager(
    case: LandingCase, tool: str
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    outsider = case.session("developer")
    grant_step_writes(case.db, outsider, case.project_id, "developer", ())
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    seam = _daemon_seam(case)
    arguments = _arguments(tool, lease.token, lease.generation)

    _status, refused = await seam(outsider, "gobby-agents", tool, arguments)
    assert isinstance(refused, dict)
    assert refused["error_code"] == "release_window_forbidden", refused

    _status, reply = await seam(owner, "gobby-agents", tool, arguments)
    assert isinstance(reply, dict)
    assert reply["success"] is True, reply
    admitted = reply["result"]
    # Only what the batch needs crosses the seam: no credentials, no raw rows.
    assert set(admitted) == PAYLOAD_KEYS[tool]
    wire = json.dumps(admitted)
    assert "database_url" not in wire.lower()
    assert "postgresql://" not in wire
    if tool == "release_batch_checkout":
        assert Path(admitted["root"]).resolve() == case.repo.resolve()
        assert admitted["candidate_sha"] == lease.candidate_sha


@pytest.fixture
def machine_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the thread of every machine-id read in the release store and window modules."""
    reads: list[int] = []

    def recording() -> str:
        reads.append(threading.get_ident())
        return require_machine_id()

    monkeypatch.setattr("gobby.releases.store.require_machine_id", recording)
    monkeypatch.setattr("gobby.releases.window.require_machine_id", recording)
    return reads


@pytest.mark.parametrize("tool", ["release_batch_checkout", "release_batch_active_runs"])
async def test_batch_reads_the_machine_off_the_event_loop(
    case: LandingCase, machine_reads: list[int], tool: str
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    loop_thread = threading.get_ident()

    _status, reply = await _daemon_seam(case)(
        owner, "gobby-agents", tool, _arguments(tool, lease.token, lease.generation)
    )

    assert isinstance(reply, dict) and reply["success"] is True, reply
    assert machine_reads, "the batch never read the machine id"
    assert loop_thread not in machine_reads


@pytest.mark.parametrize(
    ("foreign", "code"), [(False, "release_run_ids_invalid"), (True, "release_window_stale")]
)
async def test_batch_fenced_runs_checks_the_lease_then_the_run_ids(
    case: LandingCase, foreign: bool, code: str
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
    )
    arguments: dict[str, object] = {
        "token": "foreign" if foreign else lease.token,
        "generation": lease.generation,
        "run_ids": ["not-a-run-id"],
    }

    _status, refused = await _daemon_seam(case)(
        owner, "gobby-agents", "release_batch_fenced_runs", arguments
    )

    assert isinstance(refused, dict)
    assert (refused["success"], refused["error_code"]) == (False, code), refused


def test_seat_batch_reports_a_missing_baseline_as_a_refusal(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = case.session("release manager")
    grant_step_writes(case.db, owner, case.project_id, "release-manager", ())
    case.direct({"src/gobby/tasks/a.py": "A = 1\n"})
    lease = _acquire(case, owner)
    fakes = Fakes(case, Clock(datetime.now(UTC)))
    monkeypatch.chdir(case.repo)
    monkeypatch.setattr("gobby.cli.release.get_project_context", lambda: {"id": case.project_id})
    monkeypatch.setattr("gobby.cli.release._environment", lambda *_args: fakes.env())
    monkeypatch.setattr("gobby.cli.release._lease_call", _daemon_seam(case))

    result = CliRunner().invoke(
        release,
        ["batch", "--token", lease.token, "--generation", str(lease.generation)],
        env={"GOBBY_SESSION_ID": owner, "DATABASE_URL": None},
    )

    assert result.exit_code == 1
    assert "release_baseline_missing" in result.output
    assert not fakes.checks


def _acquire(case: LandingCase, owner: str) -> ReleaseWindow:
    return asyncio.run(
        acquire_window(
            case.db, project_id=case.project_id, owner_session_id=owner, now=datetime.now(UTC)
        )
    )
