"""A direct start drops the agent identity from the daemon's environment."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.cli import daemon_start


def test_direct_start_drops_agent_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    identity = {
        "GOBBY_SESSION_ID": "session",
        "GOBBY_PROJECT_ID": "project",
        "GOBBY_AGENT_RUN_ID": "run",
        "GOBBY_MANAGED_EXECUTION_ID": "execution",
        "GOBBY_AGENT_API_TOKEN": "agent-token",
        "GOBBY_RELEASE_TOKEN": "release-token",
    }
    for name, value in identity.items():
        monkeypatch.setenv(name, value)
    popen = MagicMock(side_effect=OSError("probe"))
    monkeypatch.setattr("gobby.cli.daemon_start.subprocess.Popen", popen)
    monkeypatch.setattr(daemon_start, "resolved_log_path", lambda *_args: tmp_path / "runtime.log")
    monkeypatch.setattr(daemon_start, "init_local_storage", MagicMock())
    monkeypatch.setattr(daemon_start, "is_port_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daemon_start, "resolve_native_bin", lambda _name: "/test/gdaemon")
    monkeypatch.setattr(daemon_start, "has_auth_env", lambda _cli: True)
    monkeypatch.setattr(
        daemon_start,
        "convert_held_claim_to_reservation",
        lambda *_args, **_kwargs: SimpleNamespace(nonce_path="nonce"),
    )
    monkeypatch.setattr(daemon_start, "cancel_service_reservation", MagicMock())
    config = SimpleNamespace(
        logging=None,
        daemon_port=1,
        websocket=SimpleNamespace(port=2),
        bind_host="127.0.0.1",
        ui=SimpleNamespace(enabled=False),
    )

    with pytest.raises(SystemExit):
        daemon_start._launch_direct_runner(
            object(), tmp_path / "gobby.pid", tmp_path / "home", config, verbose=False
        )

    env = popen.call_args.kwargs["env"]
    assert not identity.keys() & env.keys()
    assert env["GOBBY_HOME"] == str(tmp_path / "home")
