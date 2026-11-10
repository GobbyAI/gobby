"""Provider login homes stay normal while every Gobby client uses isolated routing."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.handoff_continuation_launch import provider_launch, seat_environment

pytestmark = pytest.mark.unit


def test_provider_homes_preserved_and_parent_seat_identity_removed(tmp_path: Path) -> None:
    ambient = {
        "HOME": "/operator/home",
        "CODEX_HOME": "/operator/codex",
        "CLAUDE_CONFIG_DIR": "/operator/claude",
        "XDG_CONFIG_HOME": "/operator/config",
        "PATH": "/operator/bin:/usr/bin:/bin",
        "GOBBY_SESSION_ID": "foreign-seat",
        "GOBBY_AGENT_RUN_ID": "foreign-run",
        "GOBBY_DAEMON_URL": "http://127.0.0.1:60887",
        "GOBBY_PORT": "60887",
        "GOBBY_HOOKS_DISABLED": "1",
    }
    actual = seat_environment(ambient, home=tmp_path, port=32145)
    for name in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        assert actual[name] == ambient[name]
    assert actual["GOBBY_HOME"] == str(tmp_path)
    assert actual["GOBBY_DAEMON_URL"] == "http://127.0.0.1:32145"
    for name in ("GOBBY_SESSION_ID", "GOBBY_AGENT_RUN_ID", "GOBBY_PORT", "GOBBY_HOOKS_DISABLED"):
        assert name not in actual
    assert ambient["GOBBY_SESSION_ID"] == "foreign-seat"


def test_defaults_do_not_invent_private_provider_config_homes(tmp_path: Path) -> None:
    actual = seat_environment({"HOME": "/operator/home"}, home=tmp_path, port=32145)
    assert actual["HOME"] == "/operator/home"
    assert "CODEX_HOME" not in actual
    assert "CLAUDE_CONFIG_DIR" not in actual


def test_real_subprocess_receives_routing_and_normal_home(tmp_path: Path) -> None:
    isolated = seat_environment({"HOME": "/operator/home"}, home=tmp_path, port=32145)
    command = provider_launch(
        [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"], isolated
    )
    result = subprocess.run(
        command,
        env={"HOME": "/temporary/daemon", "GOBBY_SESSION_ID": "foreign"},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)
    assert observed["HOME"] == "/operator/home"
    assert observed["GOBBY_HOME"] == str(tmp_path)
    assert observed["GOBBY_DAEMON_URL"] == "http://127.0.0.1:32145"
    assert "GOBBY_SESSION_ID" not in observed


def test_codex_rebuilt_clients_keep_isolated_routing(tmp_path: Path) -> None:
    env = seat_environment({"HOME": "/operator/home"}, home=tmp_path, port=32145)
    command = provider_launch(["/fixture/codex", "prompt"], env, provider="codex")
    for name in ("GOBBY_HOME", "GOBBY_DAEMON_URL"):
        value = json.dumps(env[name])
        assert f"mcp_servers.gobby.env.{name}={value}" in command
        assert f"shell_environment_policy.set.{name}={value}" in command
    assert command[-1] == "prompt"
