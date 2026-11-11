"""Session capability admission through an isolated, real daemon and native seat.

Run only in the authorized host lane. These tests never use the operator's daemon
or credentials. The provider executable is a deterministic local probe; spawning,
private launch materialization, claimant attestation and HTTP dispatch are real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.hub.protocol import HubDatabase
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    copy_daemon_api_key,
    create_host_socket_dir,
    daemon_token,
    link_spawning_operator_srt,
    stop_terminal_host,
)
from tests.e2e.test_external_terminal_attach import E2E_PROJECT_ID, _gterm_bin_dir
from tests.e2e.test_terminal_client_stack import _cancel_stack_run, _spawn_agent

pytestmark = [pytest.mark.e2e, pytest.mark.seat_refusal]

_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
# Keep the wire assertion collectable on the pre-fix base, before the issuer exists.
_SESSION_CREDENTIAL_FIELD = "_gobby_session_credential"
_MCP_CALL = {
    "server_name": "gobby-tasks",
    "tool_name": "list_ready_tasks",
    "arguments": {},
}

# Both probes use only the standard library, so the spawned sandbox needs no
# checkout imports. Report status/result only: never serialize request headers,
# private launch envelopes or minted credentials into the evidence file/stdout.
_HTTP_PROBE = """
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

def post(url, token, identity, body):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    headers.update(identity)
    request = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)
"""


def _hook_envelope(external_id: str, cwd: str, hook_type: str) -> dict[str, Any]:
    return {
        "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "critical": False,
        "source": "claude",
        "hook_type": hook_type,
        "input_data": {
            "session_id": external_id,
            "machine_id": _MACHINE_ID,
            "source": "startup",
            "cwd": cwd,
        },
    }


def _seat_probe(daemon_url: str, project_dir: Path) -> str:
    hook = _hook_envelope("", str(project_dir), "session-start")
    return (
        f"#!{sys.executable}\n"
        + _HTTP_PROBE
        + f"""
if any(arg in {{"--version", "-v"}} for arg in sys.argv[1:]):
    print("1.0.0-e2e")
    raise SystemExit(0)
import signal

# This is the actual envelope delivered by the daemon's native spawn path.
launch = Path(os.environ["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"])
assert launch.stat().st_mode & 0o077 == 0
envelope = json.loads(launch.read_text())
principal = envelope["principal"]
token = envelope["managed_api_token"]
run_id = os.environ["GOBBY_AGENT_RUN_ID"]
session_id = os.environ["GOBBY_SESSION_ID"]
assert principal["execution_id"] == run_id
assert principal["session_id"] == session_id
identity = {{
    "X-Gobby-Agent-Run-Id": run_id,
    "X-Gobby-Session-Id": session_id,
    "X-Gobby-Project-Id": principal["project_id"],
    "X-Gobby-Caller-Project-Id": principal["project_id"],
}}
hook = {hook!r}
hook["input_data"]["session_id"] = sys.argv[sys.argv.index("--session-id") + 1]
start_status, _ = post({daemon_url!r} + "/api/hooks/execute", token, identity, hook)
status, result = post({daemon_url!r} + "/api/mcp/tools/call", token, identity, {_MCP_CALL!r})
report = {{"start_status": start_status, "status": status, "result": result,
          "run_id": run_id, "session_id": session_id}}
destination = Path({str(project_dir)!r}) / (".session-auth-e2e-" + run_id + ".json")
temporary = destination.with_suffix(".tmp")
temporary.write_text(json.dumps(report))
temporary.replace(destination)
print("SESSION-AUTH-E2E-READY", flush=True)
# Keep the run alive until the test cancels it; liveness admission stays real.
while True:
    signal.pause()
"""
    )


@pytest.fixture
def e2e_home_dir(e2e_srt_spawn_home: Path) -> Path:
    return e2e_srt_spawn_home


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: HubDatabase,
    e2e_config: tuple[Path, int, int],
    e2e_project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Use the existing real native-host/SRT setup with a local provider probe."""
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_gterm_bin_dir()))
    socket_dir = create_host_socket_dir()
    daemon_home = e2e_config[0].parent
    for directory in (daemon_home, daemon_home / ".gobby", socket_dir):
        directory.mkdir(exist_ok=True)
        copy_daemon_api_key(daemon_home, directory)
    link_spawning_operator_srt(daemon_home)
    stub_dir = e2e_project_dir / ".auth-e2e-bin"
    stub_dir.mkdir()
    provider = stub_dir / "claude"
    provider.write_text(_seat_probe(f"http://127.0.0.1:{e2e_config[1]}", e2e_project_dir))
    provider.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "terminal_host.socket_dir": str(socket_dir),
                "tmux.auto_enter_approval_prompts": False,
                "tmux.auto_enter_agent_terminals": False,
                "tmux.init_timeout_seconds": 180,
            }
        ),
        source="e2e-session-capability",
    )
    try:
        yield
    finally:
        stop_terminal_host(socket_dir)
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_machine_bearer_cannot_act_as_foreign_session(
    daemon_instance: DaemonInstance,
    cli_events: CLIEventSimulator,
    postgres_db: HubDatabase,
) -> None:
    """Refuse before a selected victim can acquire the attacker's task."""
    victim = cli_events.register_session(
        f"victim-{uuid.uuid4().hex}", source="claude", project_id=E2E_PROJECT_ID
    )
    marker = f"must-not-exist-{uuid.uuid4().hex}"
    machine_token = daemon_token(daemon_instance.gobby_home)
    with httpx.Client(base_url=daemon_instance.http_url, timeout=30) as client:
        response = client.post(
            "/api/mcp/tools/call",
            headers={
                "Authorization": f"Bearer {machine_token}",
                "X-Gobby-Session-Id": str(victim["id"]),
                "X-Gobby-Project-Id": E2E_PROJECT_ID,
            },
            json={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": marker, "category": "code"},
            },
        )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "session_requires_bound_credential"
    assert machine_token not in response.text
    assert postgres_db.fetchone("SELECT id FROM tasks WHERE title = %s", (marker,)) is None


def test_spawned_seat_completes_mcp_with_its_bound_credential(
    daemon_instance: DaemonInstance,
) -> None:
    """A real native-spawned child uses its own envelope, never a machine key."""
    with httpx.Client(
        base_url=daemon_instance.http_url,
        headers={"Authorization": f"Bearer {daemon_token(daemon_instance.gobby_home)}"},
        timeout=60,
    ) as operator:
        spawned = _spawn_agent(operator)
        run_id = str(spawned["run_id"])
        destination = daemon_instance.project_dir / f".session-auth-e2e-{run_id}.json"
        try:
            wait_for_condition(
                destination.exists, timeout=90, description="spawned seat authenticated MCP result"
            )
            report = json.loads(destination.read_text())
            assert report["run_id"] == run_id
            assert report["session_id"] == spawned["child_session_id"]
            assert report["start_status"] == 200, report
            assert report["status"] == 200, report
            assert report["result"]["success"] is True, report
        finally:
            _cancel_stack_run(operator, run_id)


def test_unspawned_session_completes_mcp_with_minted_credential(
    daemon_instance: DaemonInstance,
    tmp_path: Path,
) -> None:
    """A real client socket proves its claimant, then uses only the minted token.

    A fresh startup with no resume: the token arrives on the first activity hook.
    """
    client_home = tmp_path / "unspawned-home"
    client_home.mkdir()
    external_id = f"unspawned-{uuid.uuid4().hex}"
    start = _hook_envelope(external_id, str(daemon_instance.project_dir), "session-start")
    activity = _hook_envelope(external_id, str(daemon_instance.project_dir), "user-prompt-submit")
    activity["input_data"].pop("source")
    activity["input_data"]["user_prompt"] = "Exercise the isolated session credential"
    script = (
        _HTTP_PROBE
        + f"""
inputs = json.load(sys.stdin)
url = inputs["url"]
machine_token = inputs.pop("machine_token")
start = inputs["start"]
activity = inputs["activity"]
start_status, started = post(url + "/api/hooks/execute", machine_token, {{}}, start)
# A fresh startup defers its row, so the first activity materializes it and carries
# the token. No resume is sent.
activity_status, active = post(url + "/api/hooks/execute", machine_token, {{}}, activity)
minted_by = "start" if {_SESSION_CREDENTIAL_FIELD!r} in started else "activity"
credential = started.get({_SESSION_CREDENTIAL_FIELD!r}) or active[{_SESSION_CREDENTIAL_FIELD!r}]
del machine_token
identity = {{
    "X-Gobby-Session-Id": credential["session_id"],
    "X-Gobby-Project-Id": credential["project_id"],
    "X-Gobby-Caller-Project-Id": credential["project_id"],
}}
home = Path(os.environ["GOBBY_HOME"])
assert not (home / "bootstrap.yaml").exists()
assert not (home / "local_cli_token").exists()
status, result = post(url + "/api/mcp/tools/call", credential["token"], identity, {_MCP_CALL!r})
print(json.dumps({{"activity_status": activity_status, "minted_by": minted_by,
                  "start_status": start_status, "status": status, "result": result,
                  "session_id": credential["session_id"]}}))
"""
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "url": daemon_instance.http_url,
                "machine_token": daemon_token(daemon_instance.gobby_home),
                "start": start,
                "activity": activity,
            }
        ),
        text=True,
        capture_output=True,
        timeout=150,
        cwd=daemon_instance.project_dir,
        env={
            "PATH": os.defpath,
            "HOME": str(client_home),
            "GOBBY_HOME": str(client_home),
            "GOBBY_TEST_PROTECT": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["start_status"] == 200, report
    assert report["activity_status"] == 200, report
    # The fresh startup defers its row; its first activity hook mints the token.
    assert report["minted_by"] == "activity", report
    assert report["status"] == 200, report
    assert report["result"]["success"] is True, report
    assert report["session_id"]
    assert not (client_home / "bootstrap.yaml").exists()
    assert not (client_home / "local_cli_token").exists()
