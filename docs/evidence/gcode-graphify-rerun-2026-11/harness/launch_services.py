"""Coordinator-only, owned Docker setup with pre-launch and live containment checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import prepare_daemon
import prepare_identity
from binary_set import install_binary_set
from provision_environment import (
    COMPARATOR_CASES,
    COMPOSE_PROJECT,
    CONTAINERS,
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_SOURCE_REPO,
    NETWORK,
    PORTS,
    TASK_REF,
    VOLUMES,
    _sha256_file,
    _write_json,
    external_state_snapshot,
)
from record_installations import probe
from runtime_boundary import EvidenceError, assert_owned_runtime, require, write_once
from service_safety import validate_container, validate_rendered
from validate_environment import _load_env_file
from violation_copy import copy_attempt_violations

DAEMON_PORTS = (59480, 59481, 59482)
OPERATOR_BIN_DIR = Path.home() / ".gobby/bin"
GRAPHIFY_PACKAGE = "graphifyy==0.9.103"
PYTHON_VERSION = "3.13"


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def prepare(root: Path) -> tuple[list[str], dict[str, str]]:
    ownership = json.loads((root / "ownership.json").read_text())
    require(ownership["runtime_root"] == str(root), "runtime root mismatch")
    require(ownership["owner_task"] == TASK_REF, "owner task is not the rerun task")
    command = [
        "compose",
        "--project-name",
        COMPOSE_PROJECT,
        "--env-file",
        str(root / "config/services.env"),
        "--file",
        str(root / "config/compose.yaml"),
    ]
    rendered = json.loads(docker(*command, "config", "--format", "json"))
    images = json.loads((root / "receipts/image-references.json").read_text())
    validate_rendered(rendered, images, _load_env_file(root / "config/services.env"))
    _write_json(root / "receipts/compose.rendered.json", rendered, mode=0o600)
    image_ids = {}
    for service in CONTAINERS:
        identity = json.loads(docker("image", "inspect", images[service]))[0]
        require(
            identity["Architecture"] == "arm64" and identity["Os"] == "linux",
            "check failed: identity['Architecture'] == 'arm64' and identity['Os'] == 'linux'",
        )
        require(images[service] in identity["RepoDigests"], "configured repository digest mismatch")
        image_ids[service] = identity["Id"]
    _write_json(root / "receipts/images.json", image_ids)
    return command, image_ids


def check_fresh() -> None:
    for args, names in (
        (("ps", "--all", "--format", "{{.Names}}"), set(CONTAINERS.values())),
        (("volume", "ls", "--format", "{{.Name}}"), set(VOLUMES.values())),
        (("network", "ls", "--format", "{{.Name}}"), {NETWORK}),
    ):
        require(not set(docker(*args).splitlines()).intersection(names), "owned-name collision")
    for port in PORTS.values():
        with socket.socket() as port_probe:
            try:
                port_probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise EvidenceError(f"port {port} is already in use on 127.0.0.1") from error


def _shared_monitor_dir() -> Path | None:
    # Deferred: the step-3 process loads no gobby at import time.
    from gobby.agents.srt_socket_paths import existing_srt_mux_tmpdir

    try:
        return existing_srt_mux_tmpdir()
    except OSError:
        return None


def observe(root: Path, image_ids: dict[str, str], attempt: str) -> None:
    healthy = record_services(root, image_ids)
    after = root / "isolation/external-state.after.json"
    if not after.exists():
        _write_json(after, external_state_snapshot(DEFAULT_SOURCE_REPO), mode=0o600)
    copy_attempt_violations(root, attempt, monitor_dir=_shared_monitor_dir())
    require(healthy, "owned services not healthy; retain logs and ownership")
    print("all three owned services healthy; live containment checks passed")


def record_services(root: Path, image_ids: dict[str, str]) -> bool:
    services = {}
    for service, name in CONTAINERS.items():
        inspected = json.loads(docker("inspect", name))[0]
        validate_container(inspected, service, image_ids[service])
        services[service] = {
            "container_name": name,
            "container_id": inspected["Id"],
            "image_id": inspected["Image"],
            "health": inspected["State"]["Health"]["Status"],
        }
    network = json.loads(docker("network", "inspect", NETWORK))[0]
    require(
        network["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT,
        "check failed: network['Labels']['com.docker.compose.project'] == COMPOSE_PROJECT",
    )
    for name in VOLUMES.values():
        volume = json.loads(docker("volume", "inspect", name))[0]
        require(
            volume["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT,
            "check failed: volume['Labels']['com.docker.compose.project'] == COMPOSE_PROJECT",
        )
    _write_json(
        root / "receipts/ownership-live.json",
        {
            "compose_project": COMPOSE_PROJECT,
            "containers": {name: item["container_id"] for name, item in services.items()},
            "network_id": network["Id"],
            "volumes": VOLUMES,
        },
    )
    healthy = all(item["health"] == "healthy" for item in services.values())
    _write_json(
        root / "receipts/services.json",
        {
            "compose_project": COMPOSE_PROJECT,
            "status": "ready" if healthy else "unhealthy",
            "services": services,
            "compose_sha256": _sha256_file(root / "config/compose.yaml"),
            "rendered_sha256": _sha256_file(root / "receipts/compose.rendered.json"),
        },
    )
    return healthy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "start", "observe", "provision", "teardown"))
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--attempt", default="")
    args = parser.parse_args()
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    if args.action == "teardown":
        teardown(root)
        return
    if args.action == "provision":
        require(args.attempt.isalnum(), "provision needs an alphanumeric --attempt")
        provision(root, args.attempt)
        return
    command, image_ids = prepare(root)
    if args.action in {"check", "start"}:
        check_fresh()
        print("rendered Compose, immutable local images, names and ports passed", flush=True)
    if args.action == "start":
        start_services(root, command, image_ids)
    elif args.action == "observe":
        require(
            args.attempt != "", "observe needs --attempt to copy that attempt's violation lines"
        )
        observe(root, image_ids, args.attempt)


def step_complete(receipt: Path, verify: Callable[[], object]) -> bool:
    """Skip a step only when its receipt exists and still verifies; a stale receipt fails."""
    if not receipt.exists():
        return False
    verify()
    return True


def assert_no_indexed_rows(counts: Mapping[str, int]) -> None:
    dirty = {project: count for project, count in counts.items() if count}
    require(not dirty, f"code_indexed_file_states rows exist before the first index: {dirty}")


def start_services(root: Path, command: list[str], image_ids: dict[str, str]) -> None:
    result = subprocess.run(
        ["docker", *command, "up", "--detach", "--pull", "never", "--wait", "--wait-timeout", "60"],
        capture_output=True,
        text=True,
        timeout=80,
    )
    _write_json(
        root / "logs/install/services-start.json",
        {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
        mode=0o600,
    )
    if result.returncode:
        print(f"Docker startup exit {result.returncode}; diagnostic retained under logs/install")
    healthy = record_services(root, image_ids)
    require(result.returncode == 0, "Docker startup failed")
    require(healthy, "owned services not healthy; retain logs and ownership")


def _verify_services(root: Path) -> None:
    services = json.loads((root / "receipts/services.json").read_text())
    require(services["status"] == "ready", "services receipt is not ready")
    require(
        services["compose_sha256"] == _sha256_file(root / "config/compose.yaml"),
        "compose file changed since the services receipt",
    )
    require(
        services["rendered_sha256"] == _sha256_file(root / "receipts/compose.rendered.json"),
        "rendered compose changed since the services receipt",
    )
    for service, name in CONTAINERS.items():
        inspected = json.loads(docker("inspect", name))[0]
        require(
            inspected["Id"] == services["services"][service]["container_id"],
            f"owned container changed: {name}",
        )


def _workspaces() -> list[tuple[str, Path]]:
    return [
        (f"{comparator}-{case}", Path("corpora") / comparator / case)
        for comparator, cases in COMPARATOR_CASES.items()
        for case in cases
    ]


def _assert_daemon_down() -> None:
    for port in DAEMON_PORTS:
        with socket.socket() as probe_socket:
            probe_socket.bind(("127.0.0.1", port))


def _build_environments(root: Path, attempt: str) -> None:
    uv = shutil.which("uv")
    require(uv, "uv is required to build the bakeoff environments")
    graphify_python = str(root / "tools/graphify/venv/bin/python")
    probe(
        root,
        attempt,
        "graphify-venv",
        [uv, "venv", "--python", PYTHON_VERSION, str(root / "tools/graphify/venv")],
        root,
    )
    probe(
        root,
        attempt,
        "graphify-install",
        [uv, "pip", "install", "--python", graphify_python, GRAPHIFY_PACKAGE],
        root,
    )
    probe(
        root,
        attempt,
        "gobby-sync",
        [uv, "sync", "--frozen", "--project", str(root / "sources/gobby")],
        root,
        env={"UV_PROJECT_ENVIRONMENT": str(root / "tools/gobby/venv")},
    )


def _init_workspaces(root: Path, attempt: str) -> None:
    _assert_daemon_down()
    gobby = str(root / "tools/gobby/venv/bin/gobby")
    for name, relative in _workspaces():
        probe(root, attempt, f"init-{name}", [gobby, "init", "--name", name], root / relative)


def _indexed_row_counts(root: Path) -> dict[str, int]:
    import psycopg

    services = _load_env_file(root / "config/services.env")
    counts: dict[str, int] = {}
    with psycopg.connect(
        host="127.0.0.1",
        port=PORTS["postgres"],
        dbname=services["BAKEOFF_POSTGRES_DB"],
        user=services["BAKEOFF_POSTGRES_USER"],
        password=services["BAKEOFF_POSTGRES_PASSWORD"],
        connect_timeout=5,
    ) as conn:
        for name, relative in _workspaces():
            project = json.loads((root / relative / ".gobby/project.json").read_text())["id"]
            row = conn.execute(
                "SELECT count(*) FROM code_indexed_file_states WHERE project_id = %s", (project,)
            ).fetchone()
            require(row is not None, "check failed: row is not None")
            counts[name] = int(row[0])
    return counts


def provision(root: Path, attempt: str) -> None:
    before = root / "isolation/external-state.before.json"
    if not before.exists():
        _write_json(before, external_state_snapshot(DEFAULT_SOURCE_REPO), mode=0o600)
    command, image_ids = prepare(root)
    if not step_complete(root / "receipts/services.json", lambda: _verify_services(root)):
        check_fresh()
        start_services(root, command, image_ids)
    _build_environments(root, attempt)
    set_files = prepare_daemon.run_runtime_gobby(
        root,
        prepare_daemon.isolated_environment(root, dict(os.environ)),
        "binary-set-files",
        {},
    )["set_files"]
    install_binary_set(OPERATOR_BIN_DIR, root / "gobby-home/bin", set_files)
    prepare_daemon.prepare(root)
    prepare_identity.prepare_identity(root)
    _init_workspaces(root, attempt)
    assert_no_indexed_rows(_indexed_row_counts(root))
    print("provisioned: services, environments, daemon preparation, identity and workspaces")


def teardown(root: Path) -> None:
    final = root / "isolation/external-state.final.json"
    if not final.exists():
        _write_json(final, external_state_snapshot(DEFAULT_SOURCE_REPO), mode=0o600)
    live = json.loads((root / "receipts/ownership-live.json").read_text())
    removed: dict[str, Any] = {"containers": [], "network": None, "volumes": []}
    for container_id in live["containers"].values():
        inspected = json.loads(docker("inspect", container_id))[0]
        require(inspected["Id"] == container_id, "owned container identity changed")
        require(
            inspected["Config"]["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT,
            "check failed: inspected['Config']['Labels']['com.docker.compose.project'] == COMPOSE_PROJECT",
        )
        docker("rm", "--force", container_id)
        removed["containers"].append(container_id)
    network = json.loads(docker("network", "inspect", NETWORK))[0]
    require(network["Id"] == live["network_id"], "owned network identity changed")
    require(
        network["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT,
        "check failed: network['Labels']['com.docker.compose.project'] == COMPOSE_PROJECT",
    )
    docker("network", "rm", NETWORK)
    removed["network"] = live["network_id"]
    for name in VOLUMES.values():
        volume = json.loads(docker("volume", "inspect", name))[0]
        require(
            volume["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT,
            "check failed: volume['Labels']['com.docker.compose.project'] == COMPOSE_PROJECT",
        )
        docker("volume", "rm", name)
        removed["volumes"].append(name)
    write_once(root / "receipts/teardown.json", json.dumps(removed, indent=2) + "\n", 0o600)
    print("owned containers, network and volumes removed")


if __name__ == "__main__":
    main()
