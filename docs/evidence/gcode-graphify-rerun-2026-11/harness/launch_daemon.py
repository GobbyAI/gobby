"""Launch only the owned bakeoff daemon and preserve each attempt separately."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prepare_daemon import isolated_environment, run_runtime_gobby
from provision_environment import DEFAULT_RUNTIME_ROOT, _write_json
from runtime_boundary import EvidenceError, assert_owned_runtime, contained_file, require
from runtime_gobby import hash_package_files, package_digest


@dataclass(frozen=True)
class SrtRunner:
    node: Path
    runner: Path
    monitor_socket: str | None


MONITOR_SOCKET_ENV = "GOBBY_SRT_MONITOR_SOCKET"
OPERATOR_HOME_DENY_READS = (
    ".gobby/bootstrap.yaml",
    ".gobby/.secret_kek",
    ".gobby/.secret_salt",
    ".gobby/local_cli_token",
    ".codex/auth.json",
)
# The gobby modules that steps 4, 7 and 8 import directly, from the epic tree. Before step 4,
# each one's copy must match its copy in tools/gobby/venv, or the step stops naming the module.
GOBBY_STEP_MODULES = (
    "gobby.agents.srt_monitor",
    "gobby.agents.srt_runtime",
    "gobby.storage.schema_divergence",
    "gobby.utils.native_bin",
)
# The whole gobby package is also compared before step 4: every .py file, in both copies.
GOBBY_PACKAGE = "gobby"


def daemon_argv(root: Path, bootstrap: Path) -> list[str]:
    return [
        str(root / "tools/gobby/venv/bin/python"),
        "-m",
        "gobby.runner",
        "--config",
        str(bootstrap),
    ]


def srt_policy(root: Path, home: Path) -> dict[str, Any]:
    """Confine the daemon to writes under the runtime root and to loopback sockets."""
    return {
        "network": {
            "allowedDomains": ["localhost", "127.0.0.1"],
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowLocalBinding": True,
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
        },
        "filesystem": {
            "denyRead": [str(home / path) for path in OPERATOR_HOME_DENY_READS],
            "allowRead": [str(root)],
            "allowWrite": [str(root)],
            "denyWrite": [],
            "allowGitConfig": False,
        },
    }


def srt_wrapped_argv(
    daemon: list[str], *, policy: Path, violations: Path, srt: SrtRunner
) -> list[str]:
    """Wrap the daemon argv with the SRT runner exactly as the sandbox launch does."""
    from gobby.agents.srt_runtime import SandboxLaunch

    wrapper = SandboxLaunch(
        backend="srt",
        enforced=True,
        provider_executable=daemon[0],
        node_path=str(srt.node),
        runner_path=str(srt.runner),
        policy_path=str(policy),
        violation_path=str(violations),
    )
    return wrapper.wrap(daemon)


def epic_origins(names: Sequence[str]) -> dict[str, Path | None]:
    """Where this process's gobby resolves each module: the epic tree, since the harness loads it."""
    origins: dict[str, Path | None] = {}
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except ImportError:
            spec = None
        origins[name] = Path(spec.origin) if spec is not None and spec.origin else None
    return origins


def _module_digest(path: Path | None, name: str, side: str) -> str:
    if path is None or not path.is_file():
        raise EvidenceError(f"{name}: no {side} copy to compare")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_module_copies(
    names: Sequence[str], epic: Mapping[str, Path | None], runtime: Mapping[str, Path | None]
) -> dict[str, dict[str, dict[str, str]]]:
    """Hash each module in both copies; a missing copy or a mismatch stops the step, naming it."""
    record: dict[str, dict[str, dict[str, str]]] = {}
    for name in names:
        epic_path = epic.get(name)
        runtime_path = runtime.get(name)
        epic_digest = _module_digest(epic_path, name, "epic-tree")
        runtime_digest = _module_digest(runtime_path, name, "tools/gobby/venv")
        if epic_digest != runtime_digest:
            raise EvidenceError(f"{name}: epic-tree and tools/gobby/venv copies differ")
        record[name] = {
            "epic_tree": {"path": str(epic_path), "sha256": epic_digest},
            "runtime_venv": {"path": str(runtime_path), "sha256": runtime_digest},
        }
    return record


def compare_package_copies(epic: Mapping[str, str], runtime: Mapping[str, str]) -> dict[str, Any]:
    """Fail closed unless both copies hold the same gobby files with the same bytes, naming the first path."""
    for path in sorted(set(epic) | set(runtime)):
        if path not in runtime:
            raise EvidenceError(f"{path}: only in the epic-tree gobby package")
        if path not in epic:
            raise EvidenceError(f"{path}: only in the tools/gobby/venv gobby package")
        if epic[path] != runtime[path]:
            raise EvidenceError(f"{path}: epic-tree and tools/gobby/venv copies differ")
    return {
        "files": len(epic),
        "epic_tree_digest": package_digest(epic),
        "runtime_venv_digest": package_digest(runtime),
    }


def verify_gobby_modules(root: Path, attempt: str, env: dict[str, str]) -> dict[str, Any]:
    """Record both copies of each step-4 module and of the gobby package, or stop before the daemon starts."""
    names = list(GOBBY_STEP_MODULES)
    origins = run_runtime_gobby(root, env, "module-origins", {"modules": names})["origins"]
    runtime = {name: Path(origin) if origin else None for name, origin in origins.items()}
    epic = epic_origins([*names, GOBBY_PACKAGE])
    modules = compare_module_copies(names, epic, runtime)
    package_init = epic[GOBBY_PACKAGE]
    if package_init is None:
        raise EvidenceError(f"{GOBBY_PACKAGE}: no epic-tree copy to compare")
    package = compare_package_copies(
        hash_package_files(package_init.parent),
        run_runtime_gobby(root, env, "package-files", {})["files"],
    )
    record = {"modules": modules, "package": package}
    _write_json(root / f"receipts/gobby-module-identity-{attempt}.json", record, mode=0o600)
    return record


def resolve_srt_runner(root: Path, attempt: str) -> SrtRunner:
    """Verify the pinned SRT install; on darwin, also start the shared violation monitor."""
    from gobby.agents.srt_monitor import ensure_srt_monitor
    from gobby.agents.srt_runtime import verify_srt_installation

    installation = verify_srt_installation()
    monitor = None
    if sys.platform == "darwin":
        monitor = ensure_srt_monitor(
            node=installation.node,
            runner=installation.runner,
            env=isolated_environment(root, dict(os.environ)),
            run_key=attempt,
        )
    return SrtRunner(node=installation.node, runner=installation.runner, monitor_socket=monitor)


def launch(root: Path, attempt: str, srt: SrtRunner, *, home: Path | None = None) -> None:
    require(
        attempt and all(char.isalnum() or char in "-_" for char in attempt),
        "check failed: attempt and all((char.isalnum() or char in '-_' for char in attempt))",
    )
    bootstrap = contained_file(root, "gobby-home/bootstrap.yaml")
    import yaml

    config = yaml.safe_load(bootstrap.read_text())
    require(config["bind_host"] == "127.0.0.1", "check failed: config['bind_host'] == '127.0.0.1'")
    require(
        config["daemon_port"] == 59480 and config["websocket_port"] == 59481,
        "check failed: config['daemon_port'] == 59480 and config['websocket_port'] == 59481",
    )
    env = isolated_environment(root, dict(os.environ))
    if srt.monitor_socket is not None:
        env[MONITOR_SOCKET_ENV] = srt.monitor_socket
        env["GOBBY_SRT_RUN_KEY"] = attempt
    for name in ("codex", "qwen", "claude", "droid", "grok", "agy"):
        require(shutil.which(name, path=env["PATH"]) is None, name)
    for port in (59480, 59481, 59482):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    log_path = root / f"logs/install/daemon-{attempt}.log"
    receipt = root / f"receipts/daemon-launch-{attempt}.json"
    require(
        not log_path.exists() and not receipt.exists(),
        "check failed: not log_path.exists() and (not receipt.exists())",
    )
    daemon = daemon_argv(root, bootstrap)
    policy_path = root / f"srt/settings-{attempt}.json"
    violations_path = root / f"logs/violations-{attempt}.jsonl"
    argv = srt_wrapped_argv(daemon, policy=policy_path, violations=violations_path, srt=srt)
    policy_path.parent.mkdir(mode=0o700, exist_ok=True)
    violations_path.parent.mkdir(mode=0o700, exist_ok=True)
    _write_json(policy_path, srt_policy(root, home or Path.home()), mode=0o600)
    violations_path.touch(mode=0o600, exist_ok=False)
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
            "daemon_argv": daemon,
            "sandbox": {
                "backend": "srt",
                "node": str(srt.node),
                "runner": str(srt.runner),
                "policy": str(policy_path),
                "violations": str(violations_path),
                "monitor_socket_env": MONITOR_SOCKET_ENV if srt.monitor_socket else None,
                "monitor_socket": srt.monitor_socket,
                "run_key": attempt if srt.monitor_socket else None,
            },
            "cwd": str(root / "gobby-home"),
            "log": str(log_path),
            "path": env["PATH"],
            "provider_clis_available": False,
            "owner_task": "#22948",
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
    env = isolated_environment(DEFAULT_RUNTIME_ROOT, dict(os.environ))
    verify_gobby_modules(DEFAULT_RUNTIME_ROOT, args.attempt, env)
    srt = resolve_srt_runner(DEFAULT_RUNTIME_ROOT, args.attempt)
    launch(DEFAULT_RUNTIME_ROOT, args.attempt, srt)
