"""Validate the isolated code-wiki bakeoff environment and live service path."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from provision_environment import (
    BASE_SHA,
    CHANGE_SHA,
    COMPARATOR_CASES,
    COMPOSE_PROJECT,
    CONTAINERS,
    CONTROL_EXCLUSIONS,
    DEFAULT_RUNTIME_ROOT,
    GOBBY_SHA,
    NETWORK,
    PORTS,
    SOURCE_EXCLUSIONS,
    VOLUMES,
    build_manifest,
)

GRAPHIFY_COMMIT = "c9f99018774e2e0380e9f65b3959944559a0d5f6"
GRAPHIFY_SDIST_SHA256 = "8135a5a22b6b78745aa3ab040cb3f5cecd7126eef5b4e89764404e6e75b58568"
GROK_DMG_SHA256 = "bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09"
PINS = {
    "graphify": GRAPHIFY_COMMIT,
    "understand-anything": "07edf82a04371b6f69779b067bdc8a1a8753a9db",
    "archify": "c6519401f7b91b9d43011657880893b0a8955548",
    "codewiki": "2584854d7538dc3e3e8e6839cf8590b0cd12a431",
    "opendeepwiki": "75840e5e86213ca40ace9d5036b1f52603f8d038",
    "grok-wiki": "release-0.0.38",
}
ALLOWED_EXAMPLE_ENV = {".env.example", "Buylist/.env.example", "Restocks/.env.example"}
SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "slack-token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "github-token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "openai-key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
}
MANAGED_ENV_NAMES = (
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_SESSION_ID",
    "GOBBY_PARENT_SESSION_ID",
    "GOBBY_AGENT_API_TOKEN",
    "GOBBY_DAEMON_URL",
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _assert_file_mode(path: Path, expected: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    assert actual == expected, f"{path} mode is {actual:04o}, expected {expected:04o}"


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: manifest[key]
        for key in (
            "schema_version",
            "source_commit",
            "root",
            "control_exclusions",
            "source_exclusions",
            "file_count",
            "total_bytes",
            "input_tree_sha256",
            "files",
        )
    }


def validate_manifests(root: Path) -> None:
    init = _load_json(root / "receipts" / "init.json")
    assert init["base_commit"] == BASE_SHA
    assert init["change_commit"] == CHANGE_SHA
    assert init["gobby_commit"] == GOBBY_SHA
    assert init["corpus_copy_count"] == sum(len(cases) for cases in COMPARATOR_CASES.values())

    for template, commit in (("baseline", BASE_SHA), ("change", CHANGE_SHA)):
        path = root / "templates" / template
        recorded = _load_json(root / "manifests" / "templates" / f"{template}.json")
        actual = build_manifest(path, commit, recorded["source_exclusions"])
        assert _manifest_identity(actual) == _manifest_identity(recorded)
        exclusions = {item["path"] for item in recorded["source_exclusions"]}
        assert exclusions == set(SOURCE_EXCLUSIONS)

    for comparator, cases in COMPARATOR_CASES.items():
        for case in cases:
            path = root / "corpora" / comparator / case
            manifest_path = root / "manifests" / "corpora" / comparator / f"{case}.json"
            recorded = _load_json(manifest_path)
            actual = build_manifest(path, recorded["source_commit"], recorded["source_exclusions"])
            assert _manifest_identity(actual) == _manifest_identity(recorded), (
                f"corpus manifest mismatch: {comparator}/{case}"
            )
            indexed_paths = {item["path"] for item in recorded["files"]}
            assert not indexed_paths.intersection(CONTROL_EXCLUSIONS)
            assert not any("wiki-bakeoff-code-2026-09" in path for path in indexed_paths)
            assert not any(path.endswith("/matrix.md") for path in indexed_paths)
            _validate_corpus_names(indexed_paths, comparator, case)
            _scan_secrets(path, indexed_paths)


def _validate_corpus_names(paths: set[str], comparator: str, case: str) -> None:
    forbidden = []
    for path in paths:
        name = Path(path).name.lower()
        if name == ".env" or name in {
            "credentials.json",
            "google-service-account.json",
            "answer-key.json",
            "answer-key.md",
        }:
            forbidden.append(path)
        if name.endswith((".pem", ".p12", ".pfx")):
            forbidden.append(path)
    assert not forbidden, f"forbidden inputs in {comparator}/{case}: {sorted(forbidden)}"
    assert ALLOWED_EXAMPLE_ENV <= paths


def _scan_secrets(root: Path, paths: set[str]) -> None:
    for relative in sorted(paths):
        path = root / relative
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        payload = path.read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            assert pattern.search(payload) is None, f"{label} pattern in {relative}"
    for relative in ALLOWED_EXAMPLE_ENV:
        for line in (root / relative).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            sensitive = any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
            assert not sensitive or not value, f"nonempty secret placeholder in {relative}: {key}"


def validate_isolation(root: Path) -> None:
    ownership = _load_json(root / "ownership.json")
    assert ownership["runtime_root"] == str(DEFAULT_RUNTIME_ROOT)
    assert ownership["owned_compose_project"] == COMPOSE_PROJECT
    assert set(ownership["owned_containers"]) == set(CONTAINERS.values())
    assert set(ownership["owned_volumes"]) == set(VOLUMES.values())
    assert ownership["owned_network"] == NETWORK

    before = _load_json(root / "isolation" / "external-state.before.json")
    after = _load_json(root / "isolation" / "external-state.after.json")
    assert before["source"] == after["source"], "source checkout changed"
    before_files = {item["path"]: item for item in before["stable_global_files"]}
    after_files = {item["path"]: item for item in after["stable_global_files"]}
    assert before_files == after_files, "stable global configuration, vault, or binary changed"

    _assert_file_mode(root / "config" / "services.env", 0o600)
    _assert_file_mode(root / "gobby-home" / "bootstrap.yaml", 0o600)
    runtime = _load_json(root / "config" / "runtime.json")
    assert runtime["ports"] == PORTS
    assert runtime["bind_address"] == "127.0.0.1"
    assert runtime["gobby_home"] == str(root / "gobby-home")


def validate_compose(root: Path) -> None:
    compose_path = root / "config" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert compose["name"] == COMPOSE_PROJECT
    assert set(compose["services"]) == set(CONTAINERS)
    for service, container_name in CONTAINERS.items():
        definition = compose["services"][service]
        assert definition["container_name"] == container_name
        assert "latest" not in definition["image"]
        for binding in definition["ports"]:
            assert str(binding).startswith("127.0.0.1:")
    assert compose["networks"]["default"]["name"] == NETWORK
    assert {value["name"] for value in compose["volumes"].values()} == set(VOLUMES.values())

    rendered_path = root / "receipts" / "compose.rendered.json"
    rendered = rendered_path.read_text(encoding="utf-8")
    for forbidden in (
        '"gobby-postgres"',
        "gobby_falkordb_data",
        "gobby_qdrant_data",
        "gobby_postgres_data",
        "0.0.0.0",
        ":60891",
        ":60892",
        ":6333",
        ":6334",
        ":16379",
        ":13000",
    ):
        assert forbidden not in rendered, (
            f"shared service reference in rendered compose: {forbidden}"
        )
    for owned in (*CONTAINERS.values(), *VOLUMES.values(), NETWORK):
        assert owned in rendered


def validate_installations(root: Path) -> None:
    receipt = _load_json(root / "receipts" / "installations.json")
    comparators = receipt["comparators"]
    assert set(comparators) == set(PINS)
    for name, pin in PINS.items():
        item = comparators[name]
        assert item["required_pin"] == pin
        assert item["status"] in {"installed", "blocked"}
        if item["status"] == "blocked":
            assert item["exit_code"] != 0
            assert item["error"]
            assert (root / item["log_path"]).is_file()
            assert item.get("substitution") is None

    graphify = comparators["graphify"]
    assert graphify["status"] == "installed"
    assert graphify["version"] == "0.9.55"
    assert graphify["sdist_sha256"] == GRAPHIFY_SDIST_SHA256
    assert graphify["commit_byte_identity"] == "UNVERIFIED"
    assert Path(graphify["executable"]).is_file()
    assert Path(graphify["dependency_lock"]).is_file()

    grok = comparators["grok-wiki"]
    assert grok["required_artifact_sha256"] == GROK_DMG_SHA256

    gcode = _load_json(root / "receipts" / "gcode.json")
    assert gcode["source_commit"] == GOBBY_SHA
    assert gcode["version"] == "1.7.0"
    assert gcode["contract_version"] == 8
    assert Path(gcode["executable"]) == root / "tools" / "gcode" / "bin" / "gcode"
    assert Path(gcode["executable"]).is_file()
    assert len(gcode["executable_sha256"]) == 64
    assert Path(gcode["cargo_lock"]).is_file()


def validate_receipts(root: Path) -> None:
    services = _load_json(root / "receipts" / "services.json")
    assert services["compose_project"] == COMPOSE_PROJECT
    assert services["status"] == "ready"
    assert set(services["services"]) == set(CONTAINERS)
    for service, expected_name in CONTAINERS.items():
        item = services["services"][service]
        assert item["container_name"] == expected_name
        assert item["container_id"]
        assert item["image_id"].startswith("sha256:")
        assert item["health"] == "healthy"

    daemon = _load_json(root / "receipts" / "daemon.json")
    assert daemon["status"] == "ready"
    assert daemon["gobby_home"] == str(root / "gobby-home")
    assert daemon["http_port"] == PORTS["daemon_http"]
    assert daemon["websocket_port"] == PORTS["daemon_ws"]
    assert daemon["pid"] > 1
    assert not set(daemon["environment_present"]).intersection(MANAGED_ENV_NAMES)

    grant = _load_json(root / "receipts" / "grant.json")
    assert grant["status"] == "passed"
    assert grant["source"] in {"handshake", "cache"}
    assert grant["daemon_reachable"] is True
    assert grant["unexpired"] is True
    assert grant["postgres"] == "passed"
    assert grant["qdrant"] == "passed"
    assert grant["falkordb"] == "passed"
    assert Path(grant["project_root"]) == root / "corpora" / "gcode" / "C0"
    assert grant["project_marker_excluded_from_input_manifest"] is True


def _load_env_file(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _run_probe(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(argv, env=env, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr.strip() or completed.stdout.strip()
    return completed.stdout


def _assert_port_open(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=3):
        pass


def probe_live_environment(root: Path) -> None:
    for port in PORTS.values():
        _assert_port_open(port)

    service_env = _load_env_file(root / "config" / "services.env")
    postgres_env = os.environ.copy()
    postgres_env["PGPASSWORD"] = service_env["BAKEOFF_POSTGRES_PASSWORD"]
    postgres = _run_probe(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(PORTS["postgres"]),
            "-U",
            service_env["BAKEOFF_POSTGRES_USER"],
            "-d",
            service_env["BAKEOFF_POSTGRES_DB"],
            "-Atc",
            "SELECT current_database(), current_user, "
            "EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_search'), "
            "EXISTS (SELECT 1 FROM pg_extension WHERE extname='pgaudit')",
        ],
        env=postgres_env,
    )
    assert "gobby_bakeoff_21942|gobby_bakeoff_21942|t|t" in postgres

    redis_env = os.environ.copy()
    redis_env["REDISCLI_AUTH"] = service_env["BAKEOFF_FALKORDB_PASSWORD"]
    assert (
        _run_probe(
            ["redis-cli", "-h", "127.0.0.1", "-p", str(PORTS["falkordb"]), "PING"],
            env=redis_env,
        ).strip()
        == "PONG"
    )

    with urllib.request.urlopen(
        f"http://127.0.0.1:{PORTS['qdrant_http']}/healthz", timeout=5
    ) as response:
        assert response.status == 200
    with urllib.request.urlopen(
        f"http://127.0.0.1:{PORTS['daemon_http']}/api/auth/status", timeout=5
    ) as response:
        assert response.status == 200

    gcode_env = os.environ.copy()
    for name in MANAGED_ENV_NAMES:
        gcode_env.pop(name, None)
    gcode_env["GOBBY_HOME"] = str(root / "gobby-home")
    gcode_env["GOBBY_TEST_PROTECT"] = "1"
    status = _run_probe(
        [
            str(root / "tools" / "gcode" / "bin" / "gcode"),
            "--project",
            str(root / "corpora" / "gcode" / "C0"),
            "--format",
            "json",
            "status",
        ],
        env=gcode_env,
    )
    parsed = json.loads(status)
    assert parsed.get("project_id")

    services = _load_json(root / "receipts" / "services.json")
    for service, expected_name in CONTAINERS.items():
        inspect = json.loads(_run_probe(["docker", "inspect", expected_name]))[0]
        expected = services["services"][service]
        assert inspect["Id"] == expected["container_id"]
        assert inspect["Image"] == expected["image_id"]
        assert inspect["State"]["Health"]["Status"] == "healthy"


def validate_environment(root: Path, *, live: bool) -> None:
    assert root.resolve() == DEFAULT_RUNTIME_ROOT
    validate_manifests(root)
    validate_isolation(root)
    validate_compose(root)
    validate_installations(root)
    validate_receipts(root)
    if live:
        probe_live_environment(root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--live", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    validate_environment(args.runtime_root.resolve(), live=args.live)
    print("environment validation passed" + (" (live)" if args.live else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
