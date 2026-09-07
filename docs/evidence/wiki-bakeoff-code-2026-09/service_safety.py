"""Fail-closed checks for this bakeoff's rendered Docker Compose configuration."""

from __future__ import annotations

import re
from typing import Any

from provision_environment import COMPOSE_PROJECT, CONTAINERS, NETWORK, PORTS, VOLUMES

SERVICE_PORTS = {
    "postgres": {5432: PORTS["postgres"]},
    "qdrant": {6333: PORTS["qdrant_http"], 6334: PORTS["qdrant_grpc"]},
    "falkordb": {6379: PORTS["falkordb"], 3000: PORTS["falkordb_browser"]},
}
MOUNT_TARGETS = {
    "postgres": "/var/lib/postgresql",
    "qdrant": "/qdrant/storage",
    "falkordb": "/var/lib/falkordb/data",
}


HEALTH_COMMANDS = {
    "postgres": 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"',
    "falkordb": 'redis-cli -a "$$BAKEOFF_FALKORDB_PASSWORD" PING | grep -q PONG',
    "qdrant": (
        "bash -c 'exec 3<>/dev/tcp/localhost/6333 && printf "
        '"GET /healthz HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n" >&3 '
        '&& grep -q "healthz check passed" <&3\''
    ),
}


def expected_environments(credentials: dict[str, str]) -> dict[str, dict[str, str]]:
    assert credentials["BAKEOFF_POSTGRES_DB"] == "gobby_bakeoff_21942"
    assert credentials["BAKEOFF_POSTGRES_USER"] == "gobby_bakeoff_21942"
    assert len(credentials["BAKEOFF_POSTGRES_PASSWORD"]) >= 32
    assert len(credentials["BAKEOFF_FALKORDB_PASSWORD"]) >= 32
    return {
        "postgres": {
            "POSTGRES_DB": credentials["BAKEOFF_POSTGRES_DB"],
            "POSTGRES_USER": credentials["BAKEOFF_POSTGRES_USER"],
            "POSTGRES_PASSWORD": credentials["BAKEOFF_POSTGRES_PASSWORD"],
        },
        "qdrant": {"QDRANT__LOG_LEVEL": "WARN"},
        "falkordb": {
            "REDIS_ARGS": f"--requirepass {credentials['BAKEOFF_FALKORDB_PASSWORD']} --save 3600 1 300 100",
            "BAKEOFF_FALKORDB_PASSWORD": credentials["BAKEOFF_FALKORDB_PASSWORD"],
            "FALKORDB_ARGS": "MAX_QUEUED_QUERIES 25 TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0 RESULTSET_SIZE 10000",
        },
    }


def validate_rendered(
    compose: dict[str, Any], images: dict[str, str], credentials: dict[str, str]
) -> None:
    environments = expected_environments(credentials)
    assert set(compose) <= {"name", "services", "networks", "volumes"}
    assert compose["name"] == COMPOSE_PROJECT
    assert set(compose["services"]) == set(CONTAINERS)
    network = compose["networks"]
    assert set(network) == {"default"}
    assert network["default"] in ({"name": NETWORK}, {"name": NETWORK, "ipam": {}})
    assert compose["volumes"] == {
        f"{service}-data": {"name": volume} for service, volume in VOLUMES.items()
    }
    for name, container in CONTAINERS.items():
        service = compose["services"][name]
        assert service["environment"] == environments[name], f"environment mismatch: {name}"
        assert service["healthcheck"] == {
            "test": ["CMD-SHELL", HEALTH_COMMANDS[name]],
            "timeout": "2s",
            "interval": "2s",
            "retries": 30,
        }, f"healthcheck mismatch: {name}"
        assert set(service) <= {
            "image",
            "container_name",
            "command",
            "environment",
            "ports",
            "volumes",
            "healthcheck",
            "networks",
            "entrypoint",
        }, f"unexpected service settings: {name}"
        assert service["container_name"] == container
        assert service.get("entrypoint") is None
        if name == "postgres":
            assert service["command"] == [
                "postgres",
                "-c",
                "shared_preload_libraries=pg_search,pgaudit",
                "-c",
                "pgaudit.log=none",
            ]
        else:
            assert service.get("command") is None
        assert service["image"] == images[name]
        assert re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", service["image"])
        assert service["networks"] == {"default": None}
        ports = service["ports"]
        assert len(ports) == len(SERVICE_PORTS[name])
        observed = {}
        for binding in ports:
            assert set(binding) <= {"mode", "host_ip", "target", "published", "protocol"}
            assert binding.get("mode", "ingress") == "ingress"
            assert binding["host_ip"] == "127.0.0.1"
            assert binding.get("protocol", "tcp") == "tcp"
            observed[binding["target"]] = int(binding["published"])
        assert observed == SERVICE_PORTS[name], f"unexpected ports: {name}"
        mounts = service["volumes"]
        assert len(mounts) == 1
        mount = mounts[0]
        assert set(mount) <= {"type", "source", "target", "volume"}
        assert mount["type"] == "volume"
        assert mount["source"] == f"{name}-data"
        assert mount["target"] == MOUNT_TARGETS[name]
        assert mount.get("volume", {}) in ({}, {"nocopy": True})


def validate_container(inspect: dict[str, Any], name: str, image_id: str) -> None:
    assert inspect["Name"] == f"/{CONTAINERS[name]}"
    assert inspect["Image"] == image_id
    labels = inspect["Config"]["Labels"]
    assert labels["com.docker.compose.project"] == COMPOSE_PROJECT
    assert labels["com.docker.compose.service"] == name
    host = inspect["HostConfig"]
    assert host["Privileged"] is False
    assert host.get("Binds") in (None, [], [f"{VOLUMES[name]}:{MOUNT_TARGETS[name]}:rw"])
    assert not host.get("CapAdd") and not host.get("Devices")
    assert host.get("PidMode", "") == "" and host.get("IpcMode", "private") == "private"
    assert host["NetworkMode"] == NETWORK
    assert set(inspect["NetworkSettings"]["Networks"]) == {NETWORK}
    assert host["PortBindings"] == {
        f"{target}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
        for target, port in SERVICE_PORTS[name].items()
    }
    mounts = inspect["Mounts"]
    assert len(mounts) == 1
    assert mounts[0]["Type"] == "volume"
    assert mounts[0]["Name"] == VOLUMES[name]
    assert mounts[0]["Destination"] == MOUNT_TARGETS[name]
