"""Own the benchmark's private FalkorDB and remove only namespaces recorded by their owner."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from redis import Redis
from redis.exceptions import RedisError

TEST_DSN = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
PREFIX = "gobby_test_22910_"
# Local image already used by the installed stack; pinned by digest so no run can pull.
FALKOR_IMAGE = (
    "falkordb/falkordb@sha256:e93fcd753fe612fb0a222166a0620a1ae31b826a12f223c3b6d06038d9d7a364"
)
# The installed service's module options, restated so timings share its query limits.
FALKOR_ARGS = "MAX_QUEUED_QUERIES 25 TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0 RESULTSET_SIZE 10000"
TASK_LABEL = "gobby.task=22910"
NAMESPACE_LABEL = "gobby.namespace"


def require_namespace(name: str) -> None:
    if re.fullmatch(PREFIX + r"[0-9a-f]{32}", name) is None:
        raise ValueError("Requires the parent's exact benchmark namespace")


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=True
    ).stdout.strip()


def start_falkor(namespace: str) -> dict[str, Any]:
    """Start one unauthenticated loopback FalkorDB named and labelled by its namespace."""
    require_namespace(namespace)
    container_id = docker(
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        namespace,
        "--label",
        TASK_LABEL,
        "--label",
        f"{NAMESPACE_LABEL}={namespace}",
        "--publish",
        "127.0.0.1::6379",
        "--memory",
        "1g",
        "--env",
        "BROWSER=0",
        "--env",
        f"FALKORDB_ARGS={FALKOR_ARGS}",
        FALKOR_IMAGE,
    )
    bindings = docker("port", namespace, "6379/tcp").splitlines()
    if not bindings:
        raise ValueError(f"Private FalkorDB {namespace} published no port")
    host, _, port = bindings[0].rpartition(":")
    if host != "127.0.0.1":
        raise ValueError(f"Private FalkorDB must publish only on loopback, got {host}")
    client = Redis(host=host, port=int(port), socket_timeout=2, socket_connect_timeout=2)
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                client.ping()
                break
            except RedisError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
    finally:
        client.close()
    return {
        "container": namespace,
        "container_id": container_id,
        "image": FALKOR_IMAGE,
        "port": int(port),
    }


def labelled_containers() -> list[str]:
    return docker(
        "ps", "--all", "--filter", f"label={TASK_LABEL}", "--format", "{{.Names}}"
    ).split()


def remove_falkor(namespace: str) -> None:
    """Remove the namespace's container only when its label proves this run owns it."""
    require_namespace(namespace)
    if namespace not in labelled_containers():
        return
    label = docker(
        "inspect", "--format", f'{{{{index .Config.Labels "{NAMESPACE_LABEL}"}}}}', namespace
    )
    if label != namespace:
        raise ValueError(f"Container {namespace} does not carry its owner label")
    docker("rm", "--force", "--volumes", namespace)


def cleanup_check(report: dict[str, Any]) -> dict[str, Any]:
    """Retain before/after leak counts, including after a worker's SIGKILL."""
    owned = set(report["namespaces"])
    if not owned:
        raise ValueError("Cleanup requires the parent's exact benchmark namespace ledger")
    for name in owned:
        require_namespace(name)
    root = Path(report["temporary_directory"])
    if (
        root.parent.resolve() != Path(tempfile.gettempdir()).resolve()
        or not root.name.startswith("gobby-22910-")
        or root.is_symlink()
    ):
        raise ValueError("Cleanup must stay inside the recorded temporary root")
    result: dict[str, Any] = {"owned": sorted(owned), "errors": []}
    try:
        with psycopg.connect(
            TEST_DSN,
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        ) as connection:
            query = "SELECT nspname FROM pg_namespace WHERE starts_with(nspname, %s)"
            before = [row[0] for row in connection.execute(query, (PREFIX,)).fetchall()]
            result["schemas_before"] = before
            for name in owned.intersection(before):
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
            result["schemas_after"] = [
                row[0] for row in connection.execute(query, (PREFIX,)).fetchall()
            ]
    except psycopg.Error as exc:
        result["errors"].append(f"hub cleanup: {type(exc).__name__}: {exc}")
    try:
        result["containers_before"] = labelled_containers()
        for name in owned.intersection(result["containers_before"]):
            remove_falkor(name)
        result["containers_after"] = labelled_containers()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["errors"].append(f"container cleanup: {type(exc).__name__}: {exc}")
    result["temporary_directory_before"] = root.exists()
    try:
        if root.exists():
            shutil.rmtree(root)
    except OSError as exc:
        result["errors"].append(f"temporary cleanup: {type(exc).__name__}: {exc}")
    result["temporary_directory_after"] = root.exists()
    # Foreign prefixed schemas or labelled containers fail the check and are never removed.
    result["clean"] = (
        not result["errors"]
        and result.get("schemas_after") == []
        and result.get("containers_after") == []
        and not result["temporary_directory_after"]
    )
    return result
