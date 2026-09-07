"""Launch only the owned bakeoff daemon and preserve each attempt separately."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
from pathlib import Path

from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT, _write_json
from runtime_boundary import assert_owned_runtime, contained_file


def launch(root: Path, attempt: str) -> None:
    assert attempt and all(char.isalnum() or char in "-_" for char in attempt)
    bootstrap = contained_file(root, "gobby-home/bootstrap.yaml")
    import yaml

    config = yaml.safe_load(bootstrap.read_text())
    assert config["bind_host"] == "127.0.0.1"
    assert config["daemon_port"] == 59480 and config["websocket_port"] == 59481
    env = isolated_environment(root, dict(os.environ))
    for name in ("codex", "qwen", "claude", "droid", "grok", "agy"):
        assert shutil.which(name, path=env["PATH"]) is None, name
    for port in (59480, 59481, 59482):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    log_path = root / f"logs/install/daemon-{attempt}.log"
    receipt = root / f"receipts/daemon-launch-{attempt}.json"
    assert not log_path.exists() and not receipt.exists()
    argv = [
        str(root / "tools/gobby/venv/bin/python"),
        "-m",
        "gobby.runner",
        "--config",
        str(bootstrap),
    ]
    with log_path.open("xb") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            argv,
            cwd=root / "gobby-home",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _write_json(
        receipt,
        {
            "pid": process.pid,
            "argv": argv,
            "cwd": str(root / "gobby-home"),
            "log": str(log_path),
            "path": env["PATH"],
            "provider_clis_available": False,
            "owner_task": "#21942",
        },
        mode=0o600,
    )
    print(f"owned daemon PID {process.pid}; launch receipt {receipt}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    assert_owned_runtime(DEFAULT_RUNTIME_ROOT, args.owner_session)
    launch(DEFAULT_RUNTIME_ROOT, args.attempt)
