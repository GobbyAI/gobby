"""Launch the pinned native Grok Wiki server with its documented private root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
from datetime import UTC, datetime

from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT
from runtime_boundary import assert_owned_runtime, contained_file, write_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    assert args.attempt.isalnum(), "attempt must be alphanumeric"
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    resources = root / "tools/grok-wiki/Grok-Wiki.app/Contents/Resources"
    bun = contained_file(root, resources / "bun/bun")
    bundle = contained_file(root, resources / "server/rlm-wiki.js")
    state = root / "state/grok-wiki"
    assert state.resolve() == state, "unexpected storage directory"
    state.mkdir(mode=0o700, exist_ok=True)
    assert state.is_dir(), "storage root is not a directory"
    receipt = root / f"receipts/grok-wiki-launch-{args.attempt}.json"
    log = root / f"logs/install/grok-wiki-serve-{args.attempt}.log"
    assert not receipt.exists() and not log.exists(), "attempt already exists"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 61241))
    env = isolated_environment(root, dict(os.environ))
    overrides = {
        "GROK_WIKI_ROOT": str(state),
        "GROK_WIKI_DESKTOP_APP_DATA": str(state / "desktop"),
        "RLM_WIKI_SERVER_TELEMETRY": "off",
        "RLM_WIKI_MAX_GENERATE": "1",
        "RLM_WIKI_MAX_GENERATE_PER_USER": "1",
    }
    env.update(overrides)
    argv = [str(bun), str(bundle), "serve", "--host", "127.0.0.1", "--port", "61241"]
    with log.open("x") as output:
        os.chmod(log, 0o600)
        process = subprocess.Popen(
            argv,
            cwd=state,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    payload = {
        "pid": process.pid,
        "argv": argv,
        "cwd": str(state),
        "started_at": datetime.now(UTC).isoformat(),
        "log_path": str(log.relative_to(root)),
        "environment": overrides,
        "bun_sha256": hashlib.sha256(bun.read_bytes()).hexdigest(),
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
    }
    write_once(receipt, json.dumps(payload, indent=2) + "\n", 0o600)
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
