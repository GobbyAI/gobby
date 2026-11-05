"""The activation guard refuses a spawned agent's stop, start and cutover; restart goes to the daemon route."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import MagicMock

import click
import pytest
import yaml
from click.testing import CliRunner

import gobby
from gobby.cli import cutover as cutover_cli
from gobby.cli import daemon_lifecycle, daemon_start
from gobby.cli.runtime import CliRuntime
from gobby.releases import activation
from gobby.releases.activation import agent_activation_refusal, claims_may_activate
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils import local_token
from gobby.utils.local_token import (
    GOBBY_AGENT_API_TOKEN_ENV,
    AgentApiTokenClaims,
    derive_managed_signing_key,
    issue_agent_api_token,
)

CLAIMS = AgentApiTokenClaims(
    "session-1", "project-1", "machine-1", 1, 9999999999, agent_run_id="run-1"
)
# The guard only forwards this handle to the stubbed capability lookup, so a sentinel suffices.
HUB_DATABASE = cast(HubDatabase, object())
# Captured before the autouse fixture stubs the check, so the real check can be exercised.
_REAL_CREDENTIAL_CHECK = activation.operator_credential_readable
_SPAWN_MARKERS = (
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
)
_FORGED_KEY = "forged-by-caller"


@pytest.fixture(autouse=True)
def operator_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unspawned pane can read the operator credential; tests that need otherwise override it."""
    monkeypatch.setattr(activation, "operator_credential_readable", lambda: True)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawned seat that presents a managed capability. Stop, start and cutover never read a grant."""
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    monkeypatch.setattr(activation, "read_managed_api_token", lambda: "token-1")


@pytest.mark.parametrize("operation", ["start", "stop", "restart", "cutover"])
def test_operator_pane_passes_every_operation(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)

    assert agent_activation_refusal(operation) is None


def test_blank_run_id_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "   ")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)

    assert agent_activation_refusal("restart") is None


@pytest.mark.parametrize("operation", ["start", "stop", "cutover"])
@pytest.mark.usefixtures("spawned")
def test_spawned_stop_start_and_cutover_stay_refused_even_with_a_grant(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(activation, "claims_may_activate", lambda _db, _claims, _op: True)

    refusal = agent_activation_refusal(operation)

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


@pytest.mark.usefixtures("spawned")
def test_spawned_restart_is_never_admitted_by_a_local_stop_half() -> None:
    """A spawned restart is decided by the daemon route, so no local stop may run it."""
    refusal = agent_activation_refusal("restart")

    assert refusal is not None
    assert "decided by the daemon route" in refusal


@pytest.mark.usefixtures("spawned")
def test_spawned_restart_refusal_needs_only_the_private_envelope_and_the_daemon_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://127.0.0.1:61987")

    assert activation.spawned_restart_refusal() is None


@pytest.mark.usefixtures("spawned")
def test_spawned_restart_refusal_names_a_missing_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activation, "read_managed_api_token", lambda: None)

    refusal = activation.spawned_restart_refusal()

    assert refusal is not None
    assert "no managed capability is presented for restart" in refusal


@pytest.mark.usefixtures("spawned")
@pytest.mark.parametrize("url", ["", "http://127.0.0.1", "ftp://127.0.0.1:61987"])
def test_spawned_restart_refusal_needs_a_usable_daemon_url_with_a_port(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("GOBBY_DAEMON_URL", url)

    refusal = activation.spawned_restart_refusal()

    assert refusal is not None
    assert "no daemon URL is presented" in refusal


def test_spawned_whole_daemon_refusal_names_stop_and_start() -> None:
    refusal = activation.spawned_whole_daemon_refusal()

    assert "spawned agents may not stop or start the daemon" in refusal


def test_spawned_cutover_refusal_names_cutover() -> None:
    refusal = activation.spawned_cutover_refusal()

    assert refusal.startswith("spawned_agent_refused")
    assert "spawned agents may not cut over the daemon" in refusal


def test_copied_run_id_cannot_borrow_another_seats_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = tmp_path / "envelope.json"
    envelope.write_text(
        json.dumps({"managed_api_token": "token-1", "principal": {"execution_id": "run-2"}})
    )
    envelope.chmod(0o600)
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(envelope))

    refusal = activation.spawned_restart_refusal()

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


def test_claims_without_a_run_identity_never_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def capability(_db: object, session_id: str, _capability: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(activation, "session_has_capability", capability)
    unspawned = AgentApiTokenClaims("session-1", "project-1", "machine-1", 1, 9999999999)

    assert claims_may_activate(HUB_DATABASE, unspawned, "restart") is False
    assert calls == []


@pytest.mark.parametrize("operation", ["start", "stop", "cutover"])
def test_capability_never_admits_stop_start_or_cutover(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(activation, "session_has_capability", lambda _db, _sid, _cap: True)

    assert claims_may_activate(HUB_DATABASE, CLAIMS, operation) is False


def test_capability_is_read_from_the_verified_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, str, str]] = []

    def capability(db: object, session_id: str, capability_name: str) -> bool:
        calls.append((db, session_id, capability_name))
        return True

    monkeypatch.setattr(activation, "session_has_capability", capability)
    db = HUB_DATABASE

    assert claims_may_activate(db, CLAIMS, "restart") is True
    assert calls == [(db, "session-1", "activate_daemon")]


def test_bundled_activate_daemon_is_declared_by_exactly_one_definition() -> None:
    agents = Path(gobby.__file__).parent / "install" / "shared" / "workflows" / "agents"
    holders = [
        path.name
        for path in sorted(agents.glob("*.yaml"))
        if "activate_daemon" in (yaml.safe_load(path.read_text()) or {}).get("capabilities", [])
    ]

    assert len(holders) == 1


@pytest.mark.parametrize("command", ["restart", "cutover"])
def test_activation_starts_the_daemon_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """Refusing the start after the command's own stop would leave no daemon."""
    stopped: list[bool] = []

    def stop(*_args: object, **_kwargs: object) -> bool:
        stopped.append(True)
        return True

    def run_cutover(
        _root: Path,
        _bin_dir: Path,
        *,
        stop_daemon: Callable[[], bool],
        start_daemon: Callable[[], None],
        start_refusal: object,
    ) -> None:
        stop_daemon()
        start_daemon()

    monkeypatch.setattr(
        "gobby.releases.activation.agent_activation_refusal",
        lambda *_args: "spawned_agent_refused: refused after the stop" if stopped else None,
    )
    monkeypatch.setattr(daemon_lifecycle, "restart_start_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon_lifecycle, "_do_stop", stop)
    monkeypatch.setattr(cutover_cli, "_do_stop", stop)
    monkeypatch.setattr(cutover_cli, "load_bootstrap", MagicMock())
    monkeypatch.setattr(cutover_cli, "node_daemon_refusal", lambda _bootstrap: None)
    monkeypatch.setattr(cutover_cli, "_workspace_root", lambda _path: tmp_path)
    monkeypatch.setattr(cutover_cli, "read_workspace_pin", lambda _root: {})
    monkeypatch.setattr(cutover_cli, "run_cutover", run_cutover)
    start_probe = MagicMock(return_value="probe")
    monkeypatch.setattr(daemon_start, "worktree_daemon_refusal", start_probe)
    entry, args = (
        (daemon_lifecycle.restart, ["--full"])
        if command == "restart"
        else (cutover_cli.cutover, ["--allow-dirty"])
    )

    result = CliRunner().invoke(entry, args, obj=CliRuntime(config_file=None))

    assert stopped == [True], result.output
    assert start_probe.called, result.output


@pytest.fixture
def marker_cleared_caller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Spawn markers cleared and GOBBY_HOME redirected to a readable forged bootstrap.

    Returns the account's ``.gobby`` directory, which holds no operator bootstrap yet.
    """
    for name in (*_SPAWN_MARKERS, GOBBY_AGENT_API_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    forged = tmp_path / "forged-home"
    forged.mkdir()
    (forged / "bootstrap.yaml").write_text(f"api_key: {_FORGED_KEY}\n")
    monkeypatch.setenv("GOBBY_HOME", str(forged))
    account = tmp_path / "account" / ".gobby"
    account.mkdir(parents=True)
    monkeypatch.setattr(local_token, "account_home", lambda: account.parent)
    monkeypatch.setattr(activation, "operator_credential_readable", _REAL_CREDENTIAL_CHECK)
    return account


def _must_not_reach(name: str) -> Callable[..., NoReturn]:
    def reached(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail(f"{name} was reached by a caller the guard must refuse")

    return reached


_ENTRY_POINTS: dict[str, click.Command] = {
    "start": daemon_start.start,
    "stop": daemon_lifecycle.stop,
    "restart": daemon_lifecycle.restart,
    "cutover": cutover_cli.cutover,
}


@pytest.mark.parametrize("command", ["start", "stop", "restart", "cutover"])
@pytest.mark.parametrize(
    "setting",
    ["forged-home", "forged-home-with-token", "operator-credential-with-token"],
)
def test_marker_cleared_caller_never_reaches_the_daemon(
    marker_cleared_caller: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    setting: str,
) -> None:
    if setting != "forged-home":
        monkeypatch.setenv(GOBBY_AGENT_API_TOKEN_ENV, "forged-by-caller-token")
    if setting == "operator-credential-with-token":
        (marker_cleared_caller / "bootstrap.yaml").write_text("api_key: operator-key\n")
    monkeypatch.setattr(
        daemon_lifecycle, "stop_singleton_gate", _must_not_reach("stop_singleton_gate")
    )
    monkeypatch.setattr(daemon_lifecycle, "_restart_backend", _must_not_reach("_restart_backend"))
    monkeypatch.setattr(
        daemon_start, "worktree_daemon_refusal", _must_not_reach("worktree_daemon_refusal")
    )
    monkeypatch.setattr(cutover_cli, "run_cutover", _must_not_reach("run_cutover"))

    result = CliRunner().invoke(_ENTRY_POINTS[command], [], obj=CliRuntime(config_file=None))

    assert result.exit_code != 0, result.output
    assert "spawned_agent_refused" in result.output


def test_marked_cutover_is_refused_before_any_key_or_capability_lookup(
    marker_cleared_caller: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A capability minted under the redirected home's key is refused by the marker rule alone.

    A marked caller's cutover has no local grant, so the capability lookup is never consulted.
    """
    token = issue_agent_api_token(
        derive_managed_signing_key(_FORGED_KEY),
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
        machine_id="machine-1",
        timeout_seconds=600,
    )
    envelope = tmp_path / "envelope.json"
    envelope.write_text(
        json.dumps({"managed_api_token": token, "principal": {"execution_id": "run-1"}})
    )
    envelope.chmod(0o600)
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(envelope))
    monkeypatch.setattr(activation, "claims_may_activate", _must_not_reach("claims_may_activate"))

    refusal = agent_activation_refusal("cutover")

    assert refusal == activation.spawned_cutover_refusal()


@pytest.mark.usefixtures("spawned")
def test_spawned_cutover_is_refused_at_entry_before_any_stop_or_protected_run_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirected GOBBY_HOME changes nothing: the entry refusal runs before any stop or bearer."""
    forged = tmp_path / "forged-home"
    forged.mkdir()
    (forged / "bootstrap.yaml").write_text(f"api_key: {_FORGED_KEY}\n")
    monkeypatch.setenv("GOBBY_HOME", str(forged))
    monkeypatch.setattr(cutover_cli, "_do_stop", _must_not_reach("_do_stop"))
    monkeypatch.setattr(
        cutover_cli, "_stop_admission_refusal", _must_not_reach("_stop_admission_refusal")
    )
    monkeypatch.setattr(
        cutover_cli, "fetch_protected_runs", _must_not_reach("fetch_protected_runs")
    )
    monkeypatch.setattr(cutover_cli, "run_cutover", _must_not_reach("run_cutover"))

    result = CliRunner().invoke(
        cutover_cli.cutover, ["--allow-dirty"], obj=CliRuntime(config_file=None)
    )

    assert result.exit_code != 0, result.output
    assert "spawned_agent_refused" in result.output
