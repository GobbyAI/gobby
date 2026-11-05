"""The activation guard refuses spawned agents unless a verified managed identity holds activate_daemon."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

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
from gobby.utils.local_token import AgentApiTokenClaims

CLAIMS = AgentApiTokenClaims(
    "session-1", "project-1", "machine-1", 1, 9999999999, agent_run_id="run-1"
)
# The guard only forwards this handle to the stubbed capability lookup, so a sentinel suffices.
HUB_DATABASE = cast(HubDatabase, object())


class _HubRuntime:
    def __init__(self, config_file: object = None) -> None:
        pass

    def require_database(self, *, apply_migrations: bool = True) -> object:
        return object()

    def close(self) -> None:
        pass


def _recording_runtime(opened: list[str]) -> type[_HubRuntime]:
    class RecordingRuntime(_HubRuntime):
        def __init__(self, config_file: object = None) -> None:
            opened.append("hub")

    return RecordingRuntime


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """A spawned seat whose verified managed identity is CLAIMS; the caller sets its grant."""
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_ID", raising=False)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    monkeypatch.setattr(activation, "read_managed_api_token", lambda: "token-1")
    monkeypatch.setattr(activation, "read_managed_signing_key", lambda: b"key")
    monkeypatch.setattr(activation, "classify_agent_api_token", lambda _token, _key: CLAIMS)
    monkeypatch.setattr(activation, "CliRuntime", _HubRuntime)

    def holds(granted: bool) -> None:
        monkeypatch.setattr(activation, "claims_may_activate", lambda _db, _claims, _op: granted)

    holds(False)
    return holds


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


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_spawned_stop_and_start_stay_refused_even_for_a_holder(
    spawned: Callable[[bool], None], operation: str
) -> None:
    spawned(True)

    refusal = agent_activation_refusal(operation)

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


@pytest.mark.parametrize("operation", ["restart", "cutover"])
def test_verified_holder_passes_restart_and_cutover(
    spawned: Callable[[bool], None], operation: str
) -> None:
    spawned(True)

    assert agent_activation_refusal(operation) is None


@pytest.mark.parametrize("operation", ["restart", "cutover"])
def test_verified_definition_without_the_capability_is_refused(
    spawned: Callable[[bool], None], operation: str
) -> None:
    spawned(False)

    refusal = agent_activation_refusal(operation)

    assert refusal is not None
    assert "activate_daemon" in refusal


def test_spawned_seat_without_a_managed_envelope_is_refused(
    spawned: Callable[[bool], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned(True)
    monkeypatch.setattr(activation, "read_managed_api_token", lambda: None)
    opened: list[str] = []
    monkeypatch.setattr(activation, "CliRuntime", _recording_runtime(opened))

    refusal = agent_activation_refusal("restart")

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")
    assert opened == []


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
    opened: list[str] = []
    monkeypatch.setattr(activation, "CliRuntime", _recording_runtime(opened))

    refusal = agent_activation_refusal("restart")

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")
    assert opened == []


def test_capability_that_fails_its_signature_is_refused(
    spawned: Callable[[bool], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned(True)
    monkeypatch.setattr(
        activation, "classify_agent_api_token", lambda _token, _key: "capability_invalid"
    )

    refusal = agent_activation_refusal("restart")

    assert refusal is not None
    assert "capability_invalid" in refusal


def test_capability_without_a_signing_key_is_refused(
    spawned: Callable[[bool], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned(True)
    monkeypatch.setattr(activation, "read_managed_signing_key", lambda: None)
    monkeypatch.setattr(
        activation,
        "classify_agent_api_token",
        lambda _token, key: "signing_key_unavailable" if key is None else CLAIMS,
    )

    refusal = agent_activation_refusal("restart")

    assert refusal is not None
    assert "signing_key_unavailable" in refusal


def test_claims_without_a_run_identity_never_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def capability(_db: object, session_id: str, _capability: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(activation, "session_has_capability", capability)
    unspawned = AgentApiTokenClaims("session-1", "project-1", "machine-1", 1, 9999999999)

    assert claims_may_activate(HUB_DATABASE, unspawned, "restart") is False
    assert calls == []


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_capability_never_admits_stop_or_start(
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

    assert claims_may_activate(db, CLAIMS, "cutover") is True
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
