"""The terminal env policy on the native spawn path.

Retargets the tmux spawner's AGY passthrough-strip and denied-credential specs onto
``NativeTerminalRuntime.prepare_spawn``: the argv and env it hands the gterm host.
Every credential value here is a dummy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.constants import GOBBY_TERMINAL_ID
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import TerminalSpawnRequest

pytestmark = pytest.mark.unit

AGY_DENIED = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
UNSET_AGY_DENIED = [
    "/usr/bin/env",
    "-u",
    "GEMINI_API_KEY",
    "-u",
    "GOOGLE_API_KEY",
    "-u",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "--",
]
SRT_PREFIX = [
    "/managed/node",
    "/managed/srt/runner.js",
    "--settings",
    "/tmp/policy.json",
    "--violations",
    "/tmp/violations.jsonl",
    "--",
]
# Prints its own pid, then each AGY-denied key's value or "absent".
REPORT_PID_AND_DENIED_KEYS = (
    'echo "$$"; '
    "for k in GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_APPLICATION_CREDENTIALS; "
    'do printenv "$k" || echo absent; done'
)


class _RecordingClient:
    host_epoch = "epoch-env-policy"

    def __init__(self) -> None:
        self.spawns: list[dict[str, Any]] = []

    async def ensure_connected(self) -> None:
        return None

    async def spawn(self, **fields: Any) -> dict[str, Any]:
        self.spawns.append(fields)
        return {"ok": True, "host_terminal_id": "ht-env", "pgid": 1, "start_time": 1}


async def _spawn(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    auth_cli: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    client = _RecordingClient()
    runtime = NativeTerminalRuntime(client, frame_host_epoch=client.host_epoch)
    terminal_id = uuid4()
    await runtime.prepare_spawn(
        TerminalSpawnRequest(
            terminal_id=terminal_id,
            spawn_key="gobby-native",
            command=command,
            env=env,
            reservation_id="rsv",
            reserve_key="rk",
            auth_cli=auth_cli,
        )
    )
    (fields,) = client.spawns
    sent_env: dict[str, str] = fields["env"]
    assert sent_env[GOBBY_TERMINAL_ID] == str(terminal_id)
    return list(fields["argv"]), sent_env


def _ambient_agy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in AGY_DENIED:
        monkeypatch.setenv(key, f"dummy-ambient-{key.lower()}")


@pytest.mark.asyncio
async def test_agy_spawn_unsets_denied_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _ambient_agy_keys(monkeypatch)
    caller_env = {"GEMINI_API_KEY": "dummy-caller-gemini", "EDITOR": "vi"}

    argv, env = await _spawn(["agy", "--yolo"], env=caller_env)

    assert argv == [*UNSET_AGY_DENIED, "agy", "--yolo"]
    assert not set(AGY_DENIED) & env.keys()
    assert env["EDITOR"] == "vi"
    assert caller_env["GEMINI_API_KEY"] == "dummy-caller-gemini", "caller env is copied"


@pytest.mark.asyncio
async def test_agy_inference_looks_through_the_srt_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ambient_agy_keys(monkeypatch)
    wrapped = [*SRT_PREFIX, "/opt/homebrew/bin/agy", "--yolo"]

    argv, env = await _spawn(wrapped)

    assert argv == [*UNSET_AGY_DENIED, *wrapped]
    assert not set(AGY_DENIED) & env.keys()


@pytest.mark.asyncio
async def test_explicit_auth_cli_selects_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _ambient_agy_keys(monkeypatch)

    argv, env = await _spawn(["/usr/local/bin/launcher"], auth_cli="agy")

    assert argv == [*UNSET_AGY_DENIED, "/usr/local/bin/launcher"]
    assert not set(AGY_DENIED) & env.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [["claude"], ["codex"], ["grok"], ["droid"], ["agy"], ["/bin/zsh"]],
)
async def test_every_spawn_blanks_the_daemon_virtualenv(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/daemon/.venv")
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "(daemon)")

    _argv, env = await _spawn(command, env={"VIRTUAL_ENV": "/caller/.venv"})

    assert env["VIRTUAL_ENV"] == ""
    assert env["VIRTUAL_ENV_PROMPT"] == ""


@pytest.mark.asyncio
async def test_passthrough_fills_the_inferred_cli_without_overriding_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-ambient-openai")
    caller_env = {"OPENAI_API_KEY": "dummy-caller-openai"}

    argv, env = await _spawn(["/opt/bin/codex", "--full-auto"], env=caller_env)

    assert argv == ["/opt/bin/codex", "--full-auto"], "a CLI without denied keys keeps argv"
    assert env["OPENAI_BASE_URL"] == "https://ambient.invalid/v1"
    assert env["OPENAI_API_KEY"] == "dummy-caller-openai"


@pytest.mark.asyncio
async def test_unrecognised_programs_get_no_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.invalid/v1")

    argv, env = await _spawn(["/bin/sh"])

    assert argv == ["/bin/sh"]
    assert "OPENAI_BASE_URL" not in env


@pytest.mark.asyncio
async def test_unset_prefix_execs_the_provider_in_place_without_the_keys() -> None:
    argv, _env = await _spawn(["/bin/sh", "-c", REPORT_PID_AND_DENIED_KEYS], auth_cli="agy")
    # Stands in for the gterm host's inherited daemon env.
    host_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    host_env.update({key: f"dummy-inherited-{key.lower()}" for key in AGY_DENIED})

    proc = subprocess.Popen(argv, env=host_env, stdout=subprocess.PIPE, text=True)
    stdout, _ = proc.communicate(timeout=10)

    assert proc.returncode == 0
    pid_line, *key_lines = stdout.splitlines()
    assert int(pid_line) == proc.pid, "env must exec the provider so the pane pid is the provider"
    assert key_lines == ["absent", "absent", "absent"]
