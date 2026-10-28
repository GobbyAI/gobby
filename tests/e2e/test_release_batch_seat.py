"""The Release Manager seat runs a release batch over the daemon's real tool route."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from click.testing import CliRunner, Result

from gobby.cli.release import release
from gobby.releases.smoke import SmokeResult
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import issue_agent_api_token, read_managed_signing_key
from tests.e2e.conftest import DaemonInstance, MCPTestClient
from tests.fixtures.review_approvals import grant_step_writes

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@dataclass(frozen=True)
class _Seat:
    """A Release Manager seat as its spawn leaves it: a session, its run and its envelope."""

    session_id: str
    run_id: str
    envelope: Path


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sessions the test registers belong to the e2e daemon's pinned machine.
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)


def _git(project: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _landed_candidate(project: Path) -> tuple[str, str]:
    """Commit a docs-only change on the main checkout; return (base, candidate)."""
    base = _git(project, "rev-parse", "HEAD")
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs" / "release-notes.md").write_text("# Release notes\n")
    _git(project, "add", "--", "docs/release-notes.md")
    _git(project, "commit", "-m", "docs: release notes")
    return base, _git(project, "rev-parse", "HEAD")


def _release_seat(daemon: DaemonInstance, postgres_db: HubDatabase, tmp_path: Path) -> _Seat:
    """Spawn the seat the way the daemon does: a run, its managed token and an envelope."""
    sessions = SessionManager(postgres_db)
    parent = sessions.register(
        external_id=f"release-parent-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=PROJECT_ID,
    )
    seat = sessions.register(
        external_id=f"release-seat-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=PROJECT_ID,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    grant_step_writes(postgres_db, seat.id, PROJECT_ID, "release-manager", ())
    runs = LocalAgentRunManager(postgres_db)
    run = runs.create(
        parent_session_id=parent.id,
        child_session_id=seat.id,
        claimed_session_id=seat.id,
        provider="codex",
        prompt="Run the release batch",
        run_id=str(uuid.uuid4()),
    )
    runs.start(run.id)
    signing_key = read_managed_signing_key(daemon.gobby_home / "bootstrap.yaml")
    assert signing_key is not None
    token = issue_agent_api_token(
        signing_key, agent_run_id=run.id, session_id=seat.id, project_id=PROJECT_ID
    )
    envelope = tmp_path / "managed-capability.json"
    envelope.write_text(
        json.dumps(
            {
                "managed_api_token": token,
                "principal": {
                    "execution_id": run.id,
                    "session_id": seat.id,
                    "project_id": PROJECT_ID,
                },
            }
        )
    )
    envelope.chmod(0o600)
    return _Seat(session_id=seat.id, run_id=run.id, envelope=envelope)


def _acquire_window(client: MCPTestClient, seat: _Seat) -> dict[str, Any]:
    client.session_id = seat.session_id
    client.agent_run_id = seat.run_id
    response = client.call_tool("gobby-agents", "acquire_release_window", {})
    result = cast(dict[str, Any], response["result"])
    assert "window" in result, response
    return cast(dict[str, Any], result["window"])


def _run_seat_batch(
    monkeypatch: pytest.MonkeyPatch,
    daemon: DaemonInstance,
    seat: _Seat,
    token: str,
    generation: int,
    base: str,
) -> Result:
    """Run `gobby release batch` with the seat's environment and nothing else."""
    monkeypatch.chdir(daemon.project_dir)
    return CliRunner().invoke(
        release,
        ["batch", "--token", token, "--generation", str(generation), "--base-sha", base],
        env={
            "GOBBY_SESSION_ID": seat.session_id,
            "GOBBY_AGENT_RUN_ID": seat.run_id,
            "GOBBY_PROJECT_ID": PROJECT_ID,
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(seat.envelope),
            "GOBBY_DAEMON_URL": daemon.http_url,
            "DATABASE_URL": None,
        },
    )


def _stub_host_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub only the host effects a batch has: the installed binaries, the pin, and the activation."""
    activations: list[str] = []
    monkeypatch.setattr(
        "gobby.cli.release._activate", lambda _root, _token, kind: activations.append(kind)
    )
    monkeypatch.setattr("gobby.cli.release._activation_kind", lambda _root: "restart")
    monkeypatch.setattr("gobby.cli.release._schema_pin_mismatch", lambda _root: None)
    monkeypatch.setattr(
        "gobby.releases.smoke.run_smokes",
        lambda *_args, **_kwargs: [SmokeResult("daemon", True, "isolated daemon answers")],
    )
    return activations


def test_release_seat_batch_runs_over_the_daemon_without_bootstrap(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    postgres_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, candidate = _landed_candidate(daemon_instance.project_dir)
    seat = _release_seat(daemon_instance, postgres_db, tmp_path)
    window = _acquire_window(mcp_client, seat)
    assert window["candidate_sha"] == candidate

    activations = _stub_host_side_effects(monkeypatch)
    # The seat reads no bootstrap file and no database URL; any attempt fails the batch.
    denied = Mock(side_effect=PermissionError("seat cannot read bootstrap.yaml"))
    monkeypatch.setattr("gobby.utils.daemon_url.load_bootstrap", denied)
    monkeypatch.setattr("gobby.cli.utils.init_local_storage", denied)

    result = _run_seat_batch(
        monkeypatch, daemon_instance, seat, window["token"], window["generation"], base
    )

    assert denied.call_count == 0
    assert "postgresql://" not in result.output
    assert result.exit_code == 0, (result.output, result.exception)
    record = json.loads(result.output)
    assert record["status"] == "succeeded"
    assert record["candidate_sha"] == candidate
    assert len(activations) == 1


@pytest.mark.parametrize("stale", ["foreign-token", "wrong-generation"])
def test_release_seat_batch_is_refused_over_the_daemon_for_a_stale_lease(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    postgres_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale: str,
) -> None:
    base, _candidate = _landed_candidate(daemon_instance.project_dir)
    seat = _release_seat(daemon_instance, postgres_db, tmp_path)
    window = _acquire_window(mcp_client, seat)

    activations = _stub_host_side_effects(monkeypatch)
    token = "foreign-token" if stale == "foreign-token" else window["token"]
    generation = window["generation"] + (stale == "wrong-generation")

    result = _run_seat_batch(monkeypatch, daemon_instance, seat, token, generation, base)

    assert result.exit_code != 0
    assert "release_window_stale" in result.output
    assert activations == []
