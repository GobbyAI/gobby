"""Structural service containment tests; never contact Docker."""

from copy import deepcopy
from typing import Any

import pytest
from provision_environment import COMPOSE_PROJECT, CONTAINERS, NETWORK, VOLUMES
from service_safety import (
    HEALTH_COMMANDS,
    MOUNT_TARGETS,
    SERVICE_PORTS,
    expected_environments,
    validate_container,
    validate_rendered,
)

IMAGES = dict.fromkeys(CONTAINERS, "example/image@sha256:" + "1" * 64)
CREDENTIALS = {
    "BAKEOFF_POSTGRES_DB": "gobby_bakeoff_21942",
    "BAKEOFF_POSTGRES_USER": "gobby_bakeoff_21942",
    "BAKEOFF_POSTGRES_PASSWORD": "p" * 40,
    "BAKEOFF_FALKORDB_PASSWORD": "f" * 40,
}


@pytest.fixture
def rendered() -> dict[str, Any]:
    return {
        "name": COMPOSE_PROJECT,
        "networks": {"default": {"name": NETWORK}},
        "volumes": {f"{name}-data": {"name": volume} for name, volume in VOLUMES.items()},
        "services": {
            name: {
                "image": IMAGES[name],
                "container_name": container,
                "environment": expected_environments(CREDENTIALS)[name],
                "healthcheck": {
                    "test": ["CMD-SHELL", HEALTH_COMMANDS[name]],
                    "interval": "2s",
                    "timeout": "2s",
                    "retries": 30,
                },
                "command": (
                    [
                        "postgres",
                        "-c",
                        "shared_preload_libraries=pg_search,pgaudit",
                        "-c",
                        "pgaudit.log=none",
                    ]
                    if name == "postgres"
                    else None
                ),
                "networks": {"default": None},
                "ports": [
                    {"host_ip": "127.0.0.1", "published": str(port), "target": target}
                    for target, port in SERVICE_PORTS[name].items()
                ],
                "volumes": [
                    {"type": "volume", "source": f"{name}-data", "target": MOUNT_TARGETS[name]}
                ],
            }
            for name, container in CONTAINERS.items()
        },
    }


def test_expected_internal_ports_are_safe_without_mutating_config(rendered: dict[str, Any]) -> None:
    original = deepcopy(rendered)
    validate_rendered(rendered, IMAGES, CREDENTIALS)
    assert rendered == original
    rendered["networks"]["default"]["ipam"] = {}
    rendered["services"]["postgres"]["entrypoint"] = None
    normalized = deepcopy(rendered)
    validate_rendered(rendered, IMAGES, CREDENTIALS)
    assert rendered == normalized


@pytest.mark.parametrize("setting", ["privileged", "network_mode", "build", "cap_add", "pid"])
def test_extra_service_authority_rejected(rendered: dict[str, Any], setting: str) -> None:
    rendered["services"]["postgres"][setting] = True
    with pytest.raises(AssertionError, match="unexpected service settings"):
        validate_rendered(rendered, IMAGES, CREDENTIALS)


@pytest.mark.parametrize("binding", ["public", "shared-port", "host-mount", "shared-volume"])
def test_shared_or_host_resources_rejected(rendered: dict[str, Any], binding: str) -> None:
    service = rendered["services"]["qdrant"]
    if binding == "public":
        service["ports"][0]["host_ip"] = "0.0.0.0"
    elif binding == "shared-port":
        service["ports"][0]["published"] = "6333"
    elif binding == "host-mount":
        service["volumes"][0]["type"] = "bind"
        service["volumes"][0]["source"] = "/Users/josh/.gobby"
    else:
        rendered["volumes"]["qdrant-data"]["name"] = "gobby_qdrant_data"
    with pytest.raises(AssertionError):
        validate_rendered(rendered, IMAGES, CREDENTIALS)


def test_image_receipt_does_not_authorize_mutable_image(rendered: dict[str, Any]) -> None:
    images = deepcopy(IMAGES)
    images["postgres"] = "example/image:18"
    rendered["services"]["postgres"]["image"] = images["postgres"]
    with pytest.raises(AssertionError):
        validate_rendered(rendered, images, CREDENTIALS)


@pytest.mark.parametrize("setting", ["environment", "healthcheck"])
def test_runtime_contract_tampering_is_rejected(rendered: dict[str, Any], setting: str) -> None:
    rendered["services"]["postgres"][setting] = {}
    with pytest.raises(AssertionError, match="mismatch: postgres"):
        validate_rendered(rendered, IMAGES, CREDENTIALS)


def test_live_named_volume_binding_is_not_a_host_mount() -> None:
    name = "postgres"
    image_id = "sha256:" + "1" * 64
    inspected: dict[str, Any] = {
        "Name": f"/{CONTAINERS[name]}",
        "Image": image_id,
        "Config": {
            "Labels": {
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.service": name,
            }
        },
        "HostConfig": {
            "Privileged": False,
            "Binds": [f"{VOLUMES[name]}:{MOUNT_TARGETS[name]}:rw"],
            "NetworkMode": NETWORK,
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "61234"}]},
        },
        "NetworkSettings": {"Networks": {NETWORK: {}}},
        "Mounts": [{"Type": "volume", "Name": VOLUMES[name], "Destination": MOUNT_TARGETS[name]}],
    }
    validate_container(inspected, name, image_id)
    unsafe = deepcopy(inspected)
    unsafe["HostConfig"]["Binds"] = ["/Users/josh/.gobby:/var/lib/postgresql:rw"]
    with pytest.raises(AssertionError):
        validate_container(unsafe, name, image_id)
