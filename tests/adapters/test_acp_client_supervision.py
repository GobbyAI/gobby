"""An ACP CLI dies with the runner that launched it, even when the runner is SIGKILLed."""

import select
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.integration

# Answers initialize over the stdio it was given, then ignores EOF the way a busy
# provider CLI does, so only the supervisor can end it.
_FAKE_CLI = """
import json, os, sys, time
request = json.loads(sys.stdin.readline())
pid_file = os.environ["FAKE_ACP_PID_FILE"]
with open(pid_file + ".tmp", "w") as handle:
    handle.write(str(os.getpid()))
os.replace(pid_file + ".tmp", pid_file)
response = {"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}}
print(json.dumps(response), flush=True)
time.sleep(60)
"""

_FAKE_RUNNER = """
import asyncio, sys
from gobby.adapters.acp_client import ACPClient

class FakeACPClient(ACPClient):
    cli_name = "fake-acp"
    display_name = "Fake ACP"
    prompt_timeout_env = "GOBBY_FAKE_ACP_PROMPT_TIMEOUT_SECONDS"

async def main() -> None:
    client = FakeACPClient(sys.argv[1], env_overrides={"FAKE_ACP_PID_FILE": sys.argv[2]})
    await client.start(auto_session=False)
    print("ready", flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""


def test_acp_cli_dies_with_a_sigkilled_runner(tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-acp"
    fake_cli.write_text(f"#!{sys.executable}\n{_FAKE_CLI}", encoding="utf-8")
    fake_cli.chmod(0o755)
    runner_script = tmp_path / "runner.py"
    runner_script.write_text(_FAKE_RUNNER, encoding="utf-8")
    pid_file = tmp_path / "cli.pid"
    runner_log = tmp_path / "runner.log"
    with runner_log.open("w", encoding="utf-8") as log:
        runner = subprocess.Popen(
            [sys.executable, str(runner_script), str(fake_cli), str(pid_file)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
    cli: psutil.Process | None = None
    try:
        assert runner.stdout is not None
        ready, _, _ = select.select([runner.stdout], [], [], 30)
        assert ready, runner_log.read_text(encoding="utf-8")
        assert runner.stdout.readline() == "ready\n", runner_log.read_text(encoding="utf-8")
        cli = psutil.Process(int(pid_file.read_text(encoding="utf-8")))

        runner.kill()
        runner.wait(timeout=5)

        _, alive = psutil.wait_procs([cli], timeout=5)
        assert alive == []
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        if cli is not None and cli.is_running():
            cli.kill()
