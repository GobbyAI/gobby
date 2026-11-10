"""Fail-closed checks for this bakeoff's rendered Docker Compose configuration."""

from __future__ import annotations

import re
from typing import Any

from provision_environment import COMPOSE_PROJECT, CONTAINERS, NETWORK, PORTS, VOLUMES
from runtime_boundary import require

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
    require(
        credentials["BAKEOFF_POSTGRES_DB"] == "gobby_bakeoff_22948",
        "check failed: credentials['BAKEOFF_POSTGRES_DB'] == 'gobby_bakeoff_22948'",
    )
    require(
        credentials["BAKEOFF_POSTGRES_USER"] == "gobby_bakeoff_22948",
        "check failed: credentials['BAKEOFF_POSTGRES_USER'] == 'gobby_bakeoff_22948'",
    )
    require(
        len(credentials["BAKEOFF_POSTGRES_PASSWORD"]) >= 32,
        "check failed: len(credentials['BAKEOFF_POSTGRES_PASSWORD']) >= 32",
    )
    require(
        len(credentials["BAKEOFF_FALKORDB_PASSWORD"]) >= 32,
        "check failed: len(credentials['BAKEOFF_FALKORDB_PASSWORD']) >= 32",
    )
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
    require(
        set(compose) <= {"name", "services", "networks", "volumes"},
        "check failed: set(compose) <= {'name', 'services', 'networks', 'volumes'}",
    )
    require(compose["name"] == COMPOSE_PROJECT, "check failed: compose['name'] == COMPOSE_PROJECT")
    require(
        set(compose["services"]) == set(CONTAINERS),
        "check failed: set(compose['services']) == set(CONTAINERS)",
    )
    network = compose["networks"]
    require(set(network) == {"default"}, "check failed: set(network) == {'default'}")
    require(
        network["default"] in ({"name": NETWORK}, {"name": NETWORK, "ipam": {}}),
        "check failed: network['default'] in ({'name': NETWORK}, {'name': NETWORK, 'ipam': {}})",
    )
    require(
        compose["volumes"]
        == {f"{service}-data": {"name": volume} for service, volume in VOLUMES.items()},
        "check failed: compose['volumes'] == {f'{service}-data': {'name': volume} for service, volume in VOLUMES.items()}",
    )
    for name, container in CONTAINERS.items():
        service = compose["services"][name]
        require(service["environment"] == environments[name], f"environment mismatch: {name}")
        require(
            service["healthcheck"]
            == {
                "test": ["CMD-SHELL", HEALTH_COMMANDS[name]],
                "timeout": "2s",
                "interval": "2s",
                "retries": 30,
            },
            f"healthcheck mismatch: {name}",
        )
        require(
            set(service)
            <= {
                "image",
                "container_name",
                "command",
                "environment",
                "ports",
                "volumes",
                "healthcheck",
                "networks",
                "entrypoint",
            },
            f"unexpected service settings: {name}",
        )
        require(
            service["container_name"] == container,
            "check failed: service['container_name'] == container",
        )
        require(
            service.get("entrypoint") is None, "check failed: service.get('entrypoint') is None"
        )
        if name == "postgres":
            require(
                service["command"]
                == [
                    "postgres",
                    "-c",
                    "shared_preload_libraries=pg_search,pgaudit",
                    "-c",
                    "pgaudit.log=none",
                ],
                "check failed: service['command'] == ['postgres', '-c', 'shared_preload_libraries=pg_search,pgaudit', '-c', 'pgaudit.log=none']",
            )
        else:
            require(service.get("command") is None, "check failed: service.get('command') is None")
        require(service["image"] == images[name], "check failed: service['image'] == images[name]")
        require(
            re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", service["image"]),
            "check failed: re.fullmatch('[^\\\\s@]+@sha256:[0-9a-f]{64}', service['image'])",
        )
        require(
            service["networks"] == {"default": None},
            "check failed: service['networks'] == {'default': None}",
        )
        ports = service["ports"]
        require(
            len(ports) == len(SERVICE_PORTS[name]),
            "check failed: len(ports) == len(SERVICE_PORTS[name])",
        )
        observed = {}
        for binding in ports:
            require(
                set(binding) <= {"mode", "host_ip", "target", "published", "protocol"},
                "check failed: set(binding) <= {'mode', 'host_ip', 'target', 'published', 'protocol'}",
            )
            require(
                binding.get("mode", "ingress") == "ingress",
                "check failed: binding.get('mode', 'ingress') == 'ingress'",
            )
            require(
                binding["host_ip"] == "127.0.0.1", "check failed: binding['host_ip'] == '127.0.0.1'"
            )
            require(
                binding.get("protocol", "tcp") == "tcp",
                "check failed: binding.get('protocol', 'tcp') == 'tcp'",
            )
            observed[binding["target"]] = int(binding["published"])
        require(observed == SERVICE_PORTS[name], f"unexpected ports: {name}")
        mounts = service["volumes"]
        require(len(mounts) == 1, "check failed: len(mounts) == 1")
        mount = mounts[0]
        require(
            set(mount) <= {"type", "source", "target", "volume"},
            "check failed: set(mount) <= {'type', 'source', 'target', 'volume'}",
        )
        require(mount["type"] == "volume", "check failed: mount['type'] == 'volume'")
        require(
            mount["source"] == f"{name}-data", "check failed: mount['source'] == f'{name}-data'"
        )
        require(
            mount["target"] == MOUNT_TARGETS[name],
            "check failed: mount['target'] == MOUNT_TARGETS[name]",
        )
        require(
            mount.get("volume", {}) in ({}, {"nocopy": True}),
            "check failed: mount.get('volume', {}) in ({}, {'nocopy': True})",
        )


def validate_container(inspect: dict[str, Any], name: str, image_id: str) -> None:
    require(
        inspect["Name"] == f"/{CONTAINERS[name]}",
        "check failed: inspect['Name'] == f'/{CONTAINERS[name]}'",
    )
    require(inspect["Image"] == image_id, "check failed: inspect['Image'] == image_id")
    labels = inspect["Config"]["Labels"]
    require(
        labels["com.docker.compose.project"] == COMPOSE_PROJECT,
        "check failed: labels['com.docker.compose.project'] == COMPOSE_PROJECT",
    )
    require(
        labels["com.docker.compose.service"] == name,
        "check failed: labels['com.docker.compose.service'] == name",
    )
    host = inspect["HostConfig"]
    require(host["Privileged"] is False, "check failed: host['Privileged'] is False")
    require(
        host.get("Binds") in (None, [], [f"{VOLUMES[name]}:{MOUNT_TARGETS[name]}:rw"]),
        "check failed: host.get('Binds') in (None, [], [f'{VOLUMES[name]}:{MOUNT_TARGETS[name]}:rw'])",
    )
    require(
        not host.get("CapAdd") and not host.get("Devices"),
        "check failed: not host.get('CapAdd') and (not host.get('Devices'))",
    )
    require(
        host.get("PidMode", "") == "" and host.get("IpcMode", "private") == "private",
        "check failed: host.get('PidMode', '') == '' and host.get('IpcMode', 'private') == 'private'",
    )
    require(host["NetworkMode"] == NETWORK, "check failed: host['NetworkMode'] == NETWORK")
    require(
        set(inspect["NetworkSettings"]["Networks"]) == {NETWORK},
        "check failed: set(inspect['NetworkSettings']['Networks']) == {NETWORK}",
    )
    require(
        host["PortBindings"]
        == {
            f"{target}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
            for target, port in SERVICE_PORTS[name].items()
        },
        "check failed: host['PortBindings'] == {f'{target}/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}] for target, port in SERVICE_PORTS[name].items()}",
    )
    mounts = inspect["Mounts"]
    require(len(mounts) == 1, "check failed: len(mounts) == 1")
    require(mounts[0]["Type"] == "volume", "check failed: mounts[0]['Type'] == 'volume'")
    require(mounts[0]["Name"] == VOLUMES[name], "check failed: mounts[0]['Name'] == VOLUMES[name]")
    require(
        mounts[0]["Destination"] == MOUNT_TARGETS[name],
        "check failed: mounts[0]['Destination'] == MOUNT_TARGETS[name]",
    )
