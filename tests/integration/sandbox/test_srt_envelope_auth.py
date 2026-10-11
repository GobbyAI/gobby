"""Native dummy-capability proof through the production SRT launch path.

Build first: cargo build -p gobby-core --example managed_auth_probe
Run with SRT_MANAGED_AUTH_PROBE pointing at that checkout's built example.
"""

import asyncio
import json
import os
import site
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import prepare_sandbox_launch
from gobby.cli.install_setup_srt import _cleanup_install_tree, install_srt_runtime
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal
from gobby.utils.local_token import verify_agent_api_token

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="SRT platform proof"),
]

_REPO = Path(__file__).resolve().parents[3]
_KEY = b"dummy-23923-signing-key"
_PROBE = r"""
import errno, json, os, subprocess, sys, urllib.request
from pathlib import Path
from gobby.utils.local_token import daemon_auth_headers

assert 'GOBBY_AGENT_API_TOKEN' not in os.environ
headers = daemon_auth_headers()
assert headers['X-Gobby-Agent-Run-Id'] == 'own'
request = urllib.request.Request(os.environ['GOBBY_DAEMON_URL'] + '/python', headers=headers)
with urllib.request.urlopen(request, timeout=5) as response:
    assert response.status == 200
denials = []
for foreign in sys.argv[2:]:
    try:
        Path(foreign).read_bytes()
    except OSError as error:
        assert error.errno in (errno.EACCES, errno.EPERM), error
        denials.append(True)
    else:
        raise AssertionError('foreign envelope readable')
rust = subprocess.run([sys.argv[1]], capture_output=True, text=True, timeout=10)
assert rust.returncode == 0, rust.stderr
assert rust.stdout.strip() == 'rust-auth-ok'
child = subprocess.run(
    [sys.executable, '-c', 'import os; print(os.getppid())'],
    capture_output=True, text=True, check=True, timeout=10,
)
assert int(child.stdout) == os.getpid()
ppid = subprocess.run(
    ['/bin/sh', '-c', 'ps -o ppid= -p $$'],
    capture_output=True, text=True, check=True, timeout=10,
)
assert int(ppid.stdout) == os.getpid()
print(json.dumps({'token_env_absent': True, 'python_auth': True, 'rust_auth': True,
                  'foreign_denied': denials, 'own_child': True, 'droid_ppid': True}))
"""


def test_srt_seat_auth_uses_only_its_private_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_setting = os.environ.get("SRT_MANAGED_AUTH_PROBE")
    if not probe_setting:
        pytest.skip("Build and supply the gcore managed_auth_probe example for native proof")
    rust_probe = Path(probe_setting).resolve(strict=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home = tmp_path / "home-alias"
    home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "dummy-parent-token")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider_bin = workspace / "bin"
    provider_bin.mkdir()
    (provider_bin / "droid").symlink_to(sys.executable)
    grant = GrantBundle.model_validate_json(
        (_REPO / "tests/runtime_grants/golden/direct_datastores.json").read_bytes()
    )
    managed_root = home / "runtime" / "managed-executions"
    launches = {}
    for owner in ("own", "foreign"):
        principal = GrantPrincipal(
            kind="agent_run",
            machine_id="dummy-machine",
            project_id="dummy-project",
            execution_id=owner,
            session_id="dummy-session",
        )
        launches[owner] = materialize_managed_launch(
            grant.model_copy(update={"principal": principal}),
            dest_dir=managed_root / owner,
            signing_key=_KEY,
            deadline_seconds=180,
        )
    foreign = managed_root / "foreign" / "grant.json"
    assert foreign.is_file()
    assert foreign.read_bytes() == foreign.resolve().read_bytes()
    seen: list[str] = []

    class AuthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
            claims = verify_agent_api_token(bearer, _KEY)
            valid = claims is not None and claims.agent_run_id == "own"
            self.send_response(200 if valid else 401)
            self.end_headers()
            if valid:
                seen.append(self.path)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            env = {
                **os.environ,
                **launches["own"].env,
                "PATH": f"{provider_bin}{os.pathsep}{os.environ['PATH']}",
                "PYTHONPATH": os.pathsep.join([str(_REPO / "src"), *site.getsitepackages()]),
                "GOBBY_DAEMON_URL": f"http://127.0.0.1:{port}",
            }
            env.pop("GOBBY_MANAGED_EXECUTION_ID", None)
            install_srt_runtime()
            sandbox = asyncio.run(
                prepare_sandbox_launch(
                    config=SandboxConfig(
                        enabled=True,
                        backend="srt",
                        allow_network=False,
                        extra_read_paths=[
                            str(_REPO / "src"),
                            str(Path(sys.prefix)),
                            str(rust_probe),
                        ],
                    ),
                    provider="droid",
                    workspace_path=str(workspace),
                    run_id="own",
                    resolver=None,
                    daemon_port=port,
                    websocket_port=port,
                    api_base=None,
                    env=env,
                    prewarm_pre_commit_store=False,
                )
            )
            script = workspace / "probe.py"
            script.write_text(_PROBE, encoding="utf-8")
            argv, child_env = sandbox.compose_subprocess(
                [
                    sys.executable,
                    str(script),
                    str(rust_probe),
                    str(foreign),
                    str(foreign.resolve()),
                ],
                env,
            )
            assert "GOBBY_AGENT_API_TOKEN" not in child_env
            own_token = json.loads(launches["own"].grant_path.read_bytes())["managed_api_token"]
            assert all(own_token not in argument for argument in argv)
            assert own_token not in child_env.values()
            result = subprocess.run(
                argv, cwd=workspace, env=child_env, capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                "token_env_absent": True,
                "python_auth": True,
                "rust_auth": True,
                "foreign_denied": [True, True],
                "own_child": True,
                "droid_ppid": True,
            }
            assert sorted(seen) == ["/python", "/rust"]
        finally:
            server.shutdown()
            thread.join(timeout=5)
            _cleanup_install_tree(real_home / "tools" / "srt")
