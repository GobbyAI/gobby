"""Coordinator-only, owned Docker setup with pre-launch and live containment checks."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
from pathlib import Path

from provision_environment import (
    COMPOSE_PROJECT,
    CONTAINERS,
    DEFAULT_RUNTIME_ROOT,
    NETWORK,
    PORTS,
    VOLUMES,
    _sha256_file,
    _write_json,
)
from runtime_boundary import assert_owned_runtime
from service_safety import validate_container, validate_rendered
from validate_environment import _load_env_file


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def prepare(root: Path) -> tuple[list[str], dict[str, str]]:
    ownership = json.loads((root / "ownership.json").read_text())
    assert ownership["runtime_root"] == str(root) and ownership["owner_task"] == "#21942"
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
        assert identity["Architecture"] == "arm64" and identity["Os"] == "linux"
        assert images[service] in identity["RepoDigests"], "configured repository digest mismatch"
        image_ids[service] = identity["Id"]
    _write_json(root / "receipts/images.json", image_ids)
    return command, image_ids


def check_fresh() -> None:
    for args, names in (
        (("ps", "--all", "--format", "{{.Names}}"), set(CONTAINERS.values())),
        (("volume", "ls", "--format", "{{.Name}}"), set(VOLUMES.values())),
        (("network", "ls", "--format", "{{.Name}}"), {NETWORK}),
    ):
        assert not set(docker(*args).splitlines()).intersection(names), "owned-name collision"
    for port in PORTS.values():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def observe(root: Path, image_ids: dict[str, str]) -> None:
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
    assert network["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT
    for name in VOLUMES.values():
        volume = json.loads(docker("volume", "inspect", name))[0]
        assert volume["Labels"]["com.docker.compose.project"] == COMPOSE_PROJECT
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
    assert healthy, "owned services not healthy; retain logs and ownership"
    print("all three owned services healthy; live containment checks passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "start", "observe"))
    parser.add_argument("--owner-session", required=True)
    args = parser.parse_args()
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    command, image_ids = prepare(root)
    if args.action in {"check", "start"}:
        check_fresh()
        print("rendered Compose, immutable local images, names and ports passed", flush=True)
    if args.action == "start":
        result = subprocess.run(
            [
                "docker",
                *command,
                "up",
                "--detach",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "60",
            ],
            capture_output=True,
            text=True,
            timeout=80,
        )
        _write_json(
            root / "logs/install/services-start.json",
            {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            mode=0o600,
        )
        if result.returncode:
            print(
                f"Docker startup exit {result.returncode}; diagnostic retained under logs/install"
            )
        observe(root, image_ids)
        assert result.returncode == 0, "Docker startup failed"
    elif args.action == "observe":
        observe(root, image_ids)


if __name__ == "__main__":
    main()
