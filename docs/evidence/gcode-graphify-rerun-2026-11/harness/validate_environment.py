"""Validate the isolated code-wiki bakeoff environment and live service path."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
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
from runtime_boundary import (
    EvidenceError,
    contained_file,
    file_record,
    require,
    verify_file_record,
    verify_symlink_record,
    write_once,
)
from schema_head import read_archived_gcode_version, schema_head_proof
from service_safety import validate_rendered
from verify_corpora import compare_manifests, verify_corpora

GRAPHIFY_ENTRYPOINT = "tools/graphify/venv/bin/graphify"
GCODE_BINARY = "tools/gcode/bin/gcode"
GCODE_CONTRACT_VERSION = 12
SEPTEMBER_ROOT = Path("/Users/josh/Projects/wiki-bakeoff-code-2026-09")
BINARY_SET_DIR = "gobby-home/bin"
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
        raise EvidenceError(f"expected JSON object: {path}")
    return value


def _assert_file_mode(path: Path, expected: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    require(actual == expected, f"{path} mode is {actual:04o}, expected {expected:04o}")


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
    require(init["base_commit"] == BASE_SHA, "check failed: init['base_commit'] == BASE_SHA")
    require(
        init["change_commit"] == CHANGE_SHA, "check failed: init['change_commit'] == CHANGE_SHA"
    )
    require(init["gobby_commit"] == GOBBY_SHA, "check failed: init['gobby_commit'] == GOBBY_SHA")
    require(
        init["corpus_copy_count"] == sum(len(cases) for cases in COMPARATOR_CASES.values()),
        "check failed: init['corpus_copy_count'] == sum((len(cases) for cases in COMPARATOR_CASES.values()))",
    )

    for template, commit in (("baseline", BASE_SHA), ("change", CHANGE_SHA)):
        path = root / "templates" / template
        recorded = _load_json(root / "manifests" / "templates" / f"{template}.json")
        actual = build_manifest(path, commit, recorded["source_exclusions"])
        require(
            _manifest_identity(actual) == _manifest_identity(recorded),
            "check failed: _manifest_identity(actual) == _manifest_identity(recorded)",
        )
        exclusions = {item["path"] for item in recorded["source_exclusions"]}
        require(
            exclusions == set(SOURCE_EXCLUSIONS),
            "check failed: exclusions == set(SOURCE_EXCLUSIONS)",
        )

    for comparator, cases in COMPARATOR_CASES.items():
        for case in cases:
            path = root / "corpora" / comparator / case
            manifest_path = root / "manifests" / "corpora" / comparator / f"{case}.json"
            recorded = _load_json(manifest_path)
            actual = build_manifest(path, recorded["source_commit"], recorded["source_exclusions"])
            require(
                _manifest_identity(actual) == _manifest_identity(recorded),
                f"corpus manifest mismatch: {comparator}/{case}",
            )
            indexed_paths = {item["path"] for item in recorded["files"]}
            require(
                not indexed_paths.intersection(CONTROL_EXCLUSIONS),
                "check failed: not indexed_paths.intersection(CONTROL_EXCLUSIONS)",
            )
            require(
                not any("wiki-bakeoff-code-2026-09" in path for path in indexed_paths),
                "check failed: not any(('wiki-bakeoff-code-2026-09' in path for path in indexed_paths))",
            )
            require(
                not any(path.endswith("/matrix.md") for path in indexed_paths),
                "check failed: not any((path.endswith('/matrix.md') for path in indexed_paths))",
            )
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
    require(not forbidden, f"forbidden inputs in {comparator}/{case}: {sorted(forbidden)}")
    require(ALLOWED_EXAMPLE_ENV <= paths, "check failed: ALLOWED_EXAMPLE_ENV <= paths")


def _scan_secrets(root: Path, paths: set[str]) -> None:
    for relative in sorted(paths):
        path = root / relative
        require(not path.is_symlink(), f"symlink in comparator input: {relative}")
        with path.open("rb") as stream:
            tail = b""
            while chunk := stream.read(1024 * 1024):
                payload = tail + chunk
                for label, pattern in SECRET_PATTERNS.items():
                    require(pattern.search(payload) is None, f"{label} pattern in {relative}")
                tail = payload[-128:]
    for relative in ALLOWED_EXAMPLE_ENV:
        for line in (root / relative).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            sensitive = any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
            require(not sensitive or not value, f"nonempty secret placeholder in {relative}: {key}")


def validate_isolation(root: Path) -> None:
    ownership = _load_json(root / "ownership.json")
    require(
        ownership["runtime_root"] == str(DEFAULT_RUNTIME_ROOT),
        "check failed: ownership['runtime_root'] == str(DEFAULT_RUNTIME_ROOT)",
    )
    require(
        ownership["owned_compose_project"] == COMPOSE_PROJECT,
        "check failed: ownership['owned_compose_project'] == COMPOSE_PROJECT",
    )
    require(
        set(ownership["owned_containers"]) == set(CONTAINERS.values()),
        "check failed: set(ownership['owned_containers']) == set(CONTAINERS.values())",
    )
    require(
        set(ownership["owned_volumes"]) == set(VOLUMES.values()),
        "check failed: set(ownership['owned_volumes']) == set(VOLUMES.values())",
    )
    require(
        ownership["owned_network"] == NETWORK, "check failed: ownership['owned_network'] == NETWORK"
    )

    before = _load_json(root / "isolation" / "external-state.before.json")
    after = _load_json(root / "isolation" / "external-state.after.json")
    validate_external_changes(root, before, after)
    validate_external_services(before, after)

    _assert_file_mode(root / "config" / "services.env", 0o600)
    _assert_file_mode(root / "gobby-home" / "bootstrap.yaml", 0o600)
    runtime = _load_json(root / "config" / "runtime.json")
    require(runtime["ports"] == PORTS, "check failed: runtime['ports'] == PORTS")
    require(
        runtime["bind_address"] == "127.0.0.1",
        "check failed: runtime['bind_address'] == '127.0.0.1'",
    )
    require(
        runtime["gobby_home"] == str(root / "gobby-home"),
        "check failed: runtime['gobby_home'] == str(root / 'gobby-home')",
    )


def validate_external_changes(root: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
    attribution = _load_json(contained_file(root, "isolation/concurrent-changes.json"))
    source = attribution["source"]
    require(
        source["before"] == before["source"], "check failed: source['before'] == before['source']"
    )
    require(source["after"] == after["source"], "check failed: source['after'] == after['source']")
    require(
        source["owner_session"] and source["message"],
        "check failed: source['owner_session'] and source['message']",
    )
    for key in ("repo", "status_porcelain_v1", "status_sha256"):
        require(before["source"][key] == after["source"][key], f"source {key} changed")
    history = verify_file_record(root, source["history"]).read_text()
    require(
        before["source"]["head"] in history and after["source"]["head"] in history,
        "check failed: before['source']['head'] in history and after['source']['head'] in history",
    )
    before_files = {item["path"]: item for item in before["stable_global_files"]}
    after_files = {item["path"]: item for item in after["stable_global_files"]}
    trust = attribution["codex_trust"]
    config_path = str(Path.home() / ".codex/config.toml")
    require(trust["path"] == config_path, "check failed: trust['path'] == config_path")
    original = before_files.pop(config_path)
    current = after_files.pop(config_path)
    require(before_files == after_files, "unattributed global file change")
    require(
        {k: v for k, v in original.items() if k != "sha256"}
        == {k: v for k, v in current.items() if k != "sha256"},
        "check failed: {k: v for k, v in original.items() if k != 'sha256'} == {k: v for k, v in current.items() if k != 'sha256'}",
    )
    captured = verify_file_record(root, trust["capture"])
    _assert_file_mode(captured, 0o600)
    payload = captured.read_bytes()
    require(
        hashlib.sha256(payload).hexdigest() == current["sha256"],
        "check failed: hashlib.sha256(payload).hexdigest() == current['sha256']",
    )
    old, new = trust["old_path"].encode(), trust["new_path"].encode()
    prefix = str(Path.home() / ".gobby/worktrees/gobby/wt-").encode()
    require(
        old.startswith(prefix) and new.startswith(prefix) and old != new,
        "check failed: old.startswith(prefix) and new.startswith(prefix) and (old != new)",
    )
    require(
        payload.count(new) == 1 and old not in payload,
        "check failed: payload.count(new) == 1 and old not in payload",
    )
    require(
        hashlib.sha256(payload.replace(new, old, 1)).hexdigest() == original["sha256"],
        "config difference exceeds the attributed worktree trust replacement",
    )
    require(
        trust["owner_session"] == source["owner_session"] and trust["message"],
        "check failed: trust['owner_session'] == source['owner_session'] and trust['message']",
    )


def validate_external_services(before: dict[str, Any], after: dict[str, Any]) -> None:
    snapshots = []
    for snapshot in (before, after):
        require(
            snapshot["docker_containers"]["exit_code"] == 0,
            "check failed: snapshot['docker_containers']['exit_code'] == 0",
        )
        require(
            snapshot["docker_volumes"]["exit_code"] == 0,
            "check failed: snapshot['docker_volumes']['exit_code'] == 0",
        )
        require(
            snapshot["shared_port_listeners"]["exit_code"] == 0,
            "check failed: snapshot['shared_port_listeners']['exit_code'] == 0",
        )
        snapshots.append(
            {
                row["Names"]: {
                    key: value for key, value in row.items() if key not in {"RunningFor", "Status"}
                }
                for row in map(json.loads, snapshot["docker_containers"]["stdout"].splitlines())
            }
        )
    old, new = snapshots
    require(all(new.get(name) == value for name, value in old.items()), "shared container changed")
    require(
        set(new) - set(old) == set(CONTAINERS.values()),
        "check failed: set(new) - set(old) == set(CONTAINERS.values())",
    )
    old_volumes = set(before["docker_volumes"]["stdout"].splitlines())
    new_volumes = set(after["docker_volumes"]["stdout"].splitlines())
    require(
        old_volumes <= new_volumes and new_volumes - old_volumes == set(VOLUMES.values()),
        "check failed: old_volumes <= new_volumes and new_volumes - old_volumes == set(VOLUMES.values())",
    )
    require(
        before["shared_port_listeners"] == after["shared_port_listeners"],
        "check failed: before['shared_port_listeners'] == after['shared_port_listeners']",
    )


def validate_compose(root: Path) -> None:
    compose_path = root / "config" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    require(compose["name"] == COMPOSE_PROJECT, "check failed: compose['name'] == COMPOSE_PROJECT")
    require(
        set(compose["services"]) == set(CONTAINERS),
        "check failed: set(compose['services']) == set(CONTAINERS)",
    )
    for service, container_name in CONTAINERS.items():
        definition = compose["services"][service]
        require(
            definition["container_name"] == container_name,
            "check failed: definition['container_name'] == container_name",
        )
        require(
            "latest" not in definition["image"], "check failed: 'latest' not in definition['image']"
        )
        for binding in definition["ports"]:
            require(
                str(binding).startswith("127.0.0.1:"),
                "check failed: str(binding).startswith('127.0.0.1:')",
            )
    require(
        compose["networks"]["default"]["name"] == NETWORK,
        "check failed: compose['networks']['default']['name'] == NETWORK",
    )
    require(
        {value["name"] for value in compose["volumes"].values()} == set(VOLUMES.values()),
        "check failed: {value['name'] for value in compose['volumes'].values()} == set(VOLUMES.values())",
    )

    rendered = _load_json(root / "receipts" / "compose.rendered.json")
    images = _load_json(root / "receipts" / "image-references.json")
    validate_rendered(rendered, images, _load_env_file(root / "config/services.env"))


def _validate_source_manifest(root: Path, name: str, pin: str, record: dict[str, Any]) -> None:
    manifest = _load_json(verify_file_record(root, record))
    source_root = root / "sources" / name
    require(manifest["source_commit"] == pin, "check failed: manifest['source_commit'] == pin")
    require(
        manifest["root"] == str(source_root), "check failed: manifest['root'] == str(source_root)"
    )
    require(
        manifest["file_count"] == len(manifest["files"]) > 0,
        "check failed: manifest['file_count'] == len(manifest['files']) > 0",
    )
    paths = [item["path"] for item in manifest["files"]]
    require(len(paths) == len(set(paths)), "duplicate source path")
    for item in manifest["files"]:
        path = source_root / item["path"]
        if item.get("type") == "symlink":
            _validate_source_symlink(source_root, path, item)
        else:
            verify_file_record(root, {**item, "path": str(path)})


def _validate_source_symlink(source_root: Path, path: Path, item: dict[str, Any]) -> None:
    # The manifest records a symlink by its stored target, verified against os.readlink: the
    # link is never followed as a regular file, and its target must resolve inside the source tree.
    verify_symlink_record(source_root, item)
    require(path.lstat().st_uid == os.getuid(), f"foreign-owned source symlink: {path}")
    resolved = (path.parent / os.readlink(path)).resolve()
    require(
        resolved.is_relative_to(source_root.resolve()),
        f"source symlink escapes source root: {path}",
    )
    require(resolved.is_file(), f"source symlink target is not a regular file: {path}")


def _validate_installation_checks(root: Path, checks: list[dict[str, Any]]) -> None:
    require(checks, "missing installation checks")
    for check in checks:
        require(
            check["argv"] and check["exit_code"] == 0,
            "check failed: check['argv'] and check['exit_code'] == 0",
        )
        require(
            Path(check["cwd"]).resolve().is_relative_to(root.resolve()),
            "check failed: Path(check['cwd']).resolve().is_relative_to(root.resolve())",
        )
        verify_file_record(root, check["log"])


def validate_graphify_receipt(root: Path, graphify: dict[str, Any]) -> None:
    # Graphify is unpinned: the PyPI version is recorded, not required to match a commit.
    require(graphify["status"] == "installed", "graphify is not installed")
    require(graphify["pypi_package"] == "graphifyy", "unexpected graphify PyPI package")
    version = graphify["pypi_version"]
    require(version, "missing graphify PyPI version")
    executables = [verify_file_record(root, record) for record in graphify["entrypoints"]]
    require(executables == [root / GRAPHIFY_ENTRYPOINT], "unexpected graphify entrypoint")
    _validate_installation_checks(root, graphify["checks"])
    for artifact in graphify["evidence"]:
        verify_file_record(root, artifact)
    locks = [
        verify_file_record(root, record).read_text() for record in graphify["dependency_locks"]
    ]
    pins = {line.strip() for lock in locks for line in lock.splitlines()}
    require(f"graphifyy=={version}" in pins, f"{version} absent from dependency lock")
    version_log = verify_file_record(root, graphify["version_log"]).read_text()
    require(version in version_log, f"{version} absent from version log")


def validate_gcode_receipt(root: Path, gcode: dict[str, Any]) -> None:
    require(gcode["source_commit"] == GOBBY_SHA, "gcode source_commit is not the gobby pin")
    gcode_version = read_archived_gcode_version(root)
    require(gcode["version"] == gcode_version, "gcode version is not pinned")
    require(
        gcode["contract_version"] == GCODE_CONTRACT_VERSION, "gcode contract_version is not pinned"
    )
    executable = verify_file_record(root, gcode["executable"])
    require(executable == root / GCODE_BINARY, "unexpected gcode executable")
    verify_file_record(root, gcode["gdaemon"])
    verify_file_record(root, gcode["cargo_lock"])
    _validate_source_manifest(root, "gobby", GOBBY_SHA, gcode["source_manifest"])
    _validate_installation_checks(root, gcode["checks"])
    version_log = verify_file_record(root, gcode["version_log"]).read_text()
    require(version_log.strip() == f"gcode {gcode_version}", "gcode version log mismatch")
    contract = _load_json(verify_file_record(root, gcode["contract_log"]))
    require(contract["tool"] == "gcode", "contract log is not from gcode")
    require(contract["contract_version"] == GCODE_CONTRACT_VERSION, "contract log version mismatch")


def validate_installations(root: Path) -> None:
    receipt = _load_json(contained_file(root, "receipts/installations.json"))
    comparators = receipt["comparators"]
    require(set(comparators) == {"graphify"}, "comparators are not graphify only")
    validate_graphify_receipt(root, comparators["graphify"])
    validate_gcode_receipt(root, _load_json(contained_file(root, "receipts/gcode.json")))


def validate_receipts(root: Path) -> None:
    services = _load_json(root / "receipts" / "services.json")
    require(
        services["compose_project"] == COMPOSE_PROJECT,
        "check failed: services['compose_project'] == COMPOSE_PROJECT",
    )
    require(services["status"] == "ready", "check failed: services['status'] == 'ready'")
    require(
        set(services["services"]) == set(CONTAINERS),
        "check failed: set(services['services']) == set(CONTAINERS)",
    )
    for service, expected_name in CONTAINERS.items():
        item = services["services"][service]
        require(
            item["container_name"] == expected_name,
            "check failed: item['container_name'] == expected_name",
        )
        require(item["container_id"], "check failed: item['container_id']")
        require(
            item["image_id"].startswith("sha256:"),
            "check failed: item['image_id'].startswith('sha256:')",
        )
        require(item["health"] == "healthy", "check failed: item['health'] == 'healthy'")

    proof = _load_json(contained_file(root, "receipts/runtime-verification.json"))
    require(
        proof["issued_at"] <= proof["observed_unix"] < proof["expires_at"],
        "check failed: proof['issued_at'] <= proof['observed_unix'] < proof['expires_at']",
    )
    require(
        proof["valid_signature_http"] == 200, "check failed: proof['valid_signature_http'] == 200"
    )
    require(
        proof["tampered_signature_http"] == 403,
        "check failed: proof['tampered_signature_http'] == 403",
    )
    require(
        proof["tampered_signature_reason"] == "invalid_signature",
        "check failed: proof['tampered_signature_reason'] == 'invalid_signature'",
    )
    require(
        proof["project_id"]
        == _load_json(contained_file(root, "corpora/gcode/C0/.gobby/project.json"))["id"],
        "check failed: proof['project_id'] == _load_json(contained_file(root, 'corpora/gcode/C0/.gobby/project.json'))['id']",
    )
    require(
        proof["postgres_identity"] == ["gobby_bakeoff_22948", proof["postgres_role"], "public"],
        "check failed: proof['postgres_identity'] == ['gobby_bakeoff_22948', proof['postgres_role'], 'public']",
    )
    require(
        proof["postgres_role"].startswith("gobby_ix_"),
        "check failed: proof['postgres_role'].startswith('gobby_ix_')",
    )
    require(
        proof["qdrant_url"] == "http://127.0.0.1:61235",
        "check failed: proof['qdrant_url'] == 'http://127.0.0.1:61235'",
    )
    require(
        proof["falkordb_endpoint"] == "127.0.0.1:61237",
        "check failed: proof['falkordb_endpoint'] == '127.0.0.1:61237'",
    )
    for record in proof["launch_receipts"].values():
        verify_file_record(root, record)


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
    require(completed.returncode == 0, completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def _assert_port_open(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=3):
        pass


def _validate_grant_binding(
    root: Path, grant: dict[str, Any], now: float
) -> dict[str, str | int | None]:
    from psycopg.conninfo import conninfo_to_dict

    project = _load_json(contained_file(root, "corpora/gcode/C0/.gobby/project.json"))
    require(
        grant["version"] == 2 and grant["api_contract"] == 1,
        "check failed: grant['version'] == 2 and grant['api_contract'] == 1",
    )
    require(grant["issued_at"] <= now < grant["expires_at"], "expired or future runtime grant")
    principal = grant["principal"]
    require(principal["project_id"] == project["id"], "foreign project grant")
    require(
        principal["machine_id"]
        == contained_file(root, "gobby-home/machine_id").read_text().strip(),
        "check failed: principal['machine_id'] == contained_file(root, 'gobby-home/machine_id').read_text().strip()",
    )
    require(principal["kind"] == "interactive", "check failed: principal['kind'] == 'interactive'")
    require(
        principal["session_id"] is None and principal["execution_id"] is None,
        "check failed: principal['session_id'] is None and principal['execution_id'] is None",
    )
    postgres = grant["capabilities"]["postgres"]
    require(
        postgres["mode"] == "direct" and now < postgres["valid_until"],
        "check failed: postgres['mode'] == 'direct' and now < postgres['valid_until']",
    )
    dsn = postgres["dsn"]
    require(isinstance(dsn, str), "check failed: isinstance(dsn, str)")
    connection = conninfo_to_dict(dsn)
    require(
        connection["host"] == "127.0.0.1" and connection["port"] == "61234",
        "check failed: connection['host'] == '127.0.0.1' and connection['port'] == '61234'",
    )
    require(
        connection["dbname"] == "gobby_bakeoff_22948",
        "check failed: connection['dbname'] == 'gobby_bakeoff_22948'",
    )
    user = connection["user"]
    require(isinstance(user, str), "check failed: isinstance(user, str)")
    require(user == postgres["role_name"], "check failed: user == postgres['role_name']")
    require(user.startswith("gobby_ix_"), "check failed: user.startswith('gobby_ix_')")
    qdrant = grant["capabilities"]["qdrant"]
    require(
        qdrant["mode"] == "direct" and qdrant["url"] == "http://127.0.0.1:61235",
        "check failed: qdrant['mode'] == 'direct' and qdrant['url'] == 'http://127.0.0.1:61235'",
    )
    falkor = grant["capabilities"]["falkordb"]
    require(falkor["mode"] == "direct", "check failed: falkor['mode'] == 'direct'")
    require(
        falkor["host"] == "127.0.0.1" and falkor["port"] == 61237,
        "check failed: falkor['host'] == '127.0.0.1' and falkor['port'] == 61237",
    )
    return connection


def _present_grant(root: Path, grant: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    encoded = base64.urlsafe_b64encode(json.dumps(grant).encode()).decode().rstrip("=")
    token = contained_file(root, "gobby-home/local_cli_token").read_text().strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORTS['daemon_http']}/api/runtime/config",
        headers={"X-Gobby-Runtime-Grant": encoded, "X-Gobby-Local-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def validate_live_processes(root: Path, launches: dict[str, Any]) -> None:
    import psutil

    require(set(launches) == {"daemon"}, "check failed: set(launches) == {'daemon'}")
    expected_ports = {"daemon": {59480, 59481}}
    for name, ports in expected_ports.items():
        launch = _load_json(verify_file_record(root, launches[name]))
        process = psutil.Process(launch["pid"])
        require(
            process.uids().real == os.getuid(), "check failed: process.uids().real == os.getuid()"
        )
        require(process.cwd() == launch["cwd"], "check failed: process.cwd() == launch['cwd']")
        require(
            Path(process.cwd()).resolve().is_relative_to(root),
            "check failed: Path(process.cwd()).resolve().is_relative_to(root)",
        )
        require(
            process.cmdline() == launch["argv"], "check failed: process.cmdline() == launch['argv']"
        )
        environment = process.environ()
        require(
            not set(environment).intersection(MANAGED_ENV_NAMES),
            "check failed: not set(environment).intersection(MANAGED_ENV_NAMES)",
        )
        require(
            environment["GOBBY_HOME"] == str(root / "gobby-home"),
            "check failed: environment['GOBBY_HOME'] == str(root / 'gobby-home')",
        )
        require(
            environment["PATH"] == launch["path"],
            "check failed: environment['PATH'] == launch['path']",
        )
        require(
            environment["GOBBY_NATIVE_BIN_DIR"] == str(root / "gobby-home/bin"),
            "check failed: environment['GOBBY_NATIVE_BIN_DIR'] == str(root / 'gobby-home/bin')",
        )
        listeners = {
            (connection.laddr.ip, connection.laddr.port)
            for connection in process.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN
        }
        require(listeners == {("127.0.0.1", port) for port in ports}, name)


def probe_live_environment(root: Path) -> dict[str, Any]:
    import psycopg
    import redis
    from prepare_daemon import isolated_environment
    from runtime_boundary import file_record

    for port in PORTS.values():
        _assert_port_open(port)
    gcode_env = isolated_environment(root, dict(os.environ))
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
    project_root = root / "corpora/gcode/C0"
    project_id = _load_json(contained_file(root, project_root / ".gobby/project.json"))["id"]
    require(
        parsed["id"] == project_id and parsed["root_path"] == str(project_root),
        "check failed: parsed['id'] == project_id and parsed['root_path'] == str(project_root)",
    )
    require(
        parsed["code_index"]["healthy"] is True,
        "check failed: parsed['code_index']['healthy'] is True",
    )

    caches = list((root / "gobby-home/grants").glob(f"*/{project_id}.json"))
    require(len(caches) == 1, "check failed: len(caches) == 1")
    cache = contained_file(root, caches[0])
    _assert_file_mode(cache, 0o600)
    grant = _load_json(cache)["grant"]
    now = time.time()
    connection = _validate_grant_binding(root, grant, now)
    postgres_dsn = grant["capabilities"]["postgres"]["dsn"]
    postgres_user = connection["user"]
    require(isinstance(postgres_dsn, str), "check failed: isinstance(postgres_dsn, str)")
    require(isinstance(postgres_user, str), "check failed: isinstance(postgres_user, str)")
    status_code, config = _present_grant(root, grant)
    require(
        status_code == 200 and config["config_revision"] == grant["config_revision"],
        "check failed: status_code == 200 and config['config_revision'] == grant['config_revision']",
    )
    tampered = copy.deepcopy(grant)
    signature = tampered["signature"]
    tampered["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    rejected, reason = _present_grant(root, tampered)
    require(
        rejected == 403 and reason["code"] == "invalid_signature",
        "check failed: rejected == 403 and reason['code'] == 'invalid_signature'",
    )
    with psycopg.connect(postgres_dsn, connect_timeout=5) as conn:
        identity = conn.execute(
            "SELECT current_database(), current_user, current_schema()"
        ).fetchone()
        require(
            identity == ("gobby_bakeoff_22948", postgres_user, "public"),
            "check failed: identity == ('gobby_bakeoff_22948', postgres_user, 'public')",
        )
        extensions = conn.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('pg_search', 'pgaudit')"
        ).fetchall()
        require(
            {row[0] for row in extensions} == {"pg_search", "pgaudit"},
            "check failed: {row[0] for row in extensions} == {'pg_search', 'pgaudit'}",
        )
    falkor = grant["capabilities"]["falkordb"]
    with redis.Redis(
        host=falkor["host"],
        port=falkor["port"],
        password=falkor["password"],
        socket_timeout=5,
        socket_connect_timeout=5,
    ) as client:
        require(client.ping(), "check failed: client.ping()")
    qdrant = grant["capabilities"]["qdrant"]
    headers = {"api-key": qdrant["api_key"]} if qdrant.get("api_key") else {}
    with urllib.request.urlopen(
        urllib.request.Request(qdrant["url"] + "/collections", headers=headers), timeout=5
    ) as response:
        require(response.status == 200, "check failed: response.status == 200")

    # The observe receipt replaces live Docker probes: it recorded each owned container's
    # identity, validated at observation time, and the ownership record it was written with.
    services = _load_json(root / "receipts" / "services.json")
    live = _load_json(contained_file(root, "receipts/ownership-live.json"))
    require(
        services["status"] == "ready" and services["compose_project"] == COMPOSE_PROJECT,
        "check failed: services['status'] == 'ready' and services['compose_project'] == COMPOSE_PROJECT",
    )
    for service, expected_name in CONTAINERS.items():
        expected = services["services"][service]
        require(
            expected["container_name"] == expected_name,
            "check failed: expected['container_name'] == expected_name",
        )
        require(expected["health"] == "healthy", "check failed: expected['health'] == 'healthy'")
        require(
            live["containers"][service] == expected["container_id"],
            "check failed: live['containers'][service] == expected['container_id']",
        )
    launches = _load_json(contained_file(root, "receipts/active-launches.json"))
    for record in launches.values():
        verify_file_record(root, record)
    validate_live_processes(root, launches)
    return {
        "observed_unix": now,
        "issued_at": grant["issued_at"],
        "expires_at": grant["expires_at"],
        "project_id": project_id,
        "valid_signature_http": status_code,
        "tampered_signature_http": rejected,
        "tampered_signature_reason": reason["code"],
        "postgres_identity": list(identity),
        "postgres_role": postgres_user,
        "qdrant_url": qdrant["url"],
        "falkordb_endpoint": "127.0.0.1:61237",
        "launch_receipts": launches,
        "gcode_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "grant_cache_at_observation": file_record(root, cache),
    }


def validate_corpora_live(
    root: Path,
    september_root: Path,
    cases: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Fail the live run unless the corpora match the September manifests and their files."""
    compare_manifests(root, september_root, cases)
    report = verify_corpora(root, cases)
    differing = sorted(name for name, item in report.items() if item["missing"] or item["changed"])
    require(not differing, f"corpora differ from their manifests: {differing}")
    return report


def binary_set_proof(root: Path) -> dict[str, Any]:
    """Record the isolated binary set; an empty or refused set fails, never passes vacuously."""
    from gobby.storage.schema_divergence import binary_set_apply_refusal
    from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

    members: dict[str, Any] = {}
    for member in SET_MEMBERS:
        path = f"{BINARY_SET_DIR}/{native_bin_name(member)}"
        require((root / path).is_file(), f"isolated binary set is missing {member}")
        members[member] = file_record(root, path)
    stamp = f"{BINARY_SET_DIR}/{IDENTITY_STAMP_NAME}"
    require((root / stamp).is_file(), "isolated binary set has no identity stamp")
    refusal = binary_set_apply_refusal(root / BINARY_SET_DIR)
    require(refusal is None, f"binary set apply refused: {refusal}")
    return {"refusal": None, "members": members, "stamp": file_record(root, stamp)}


def validate_environment(root: Path, *, live: bool) -> None:
    require(
        root.resolve() == DEFAULT_RUNTIME_ROOT,
        "check failed: root.resolve() == DEFAULT_RUNTIME_ROOT",
    )
    validate_manifests(root)
    validate_isolation(root)
    validate_compose(root)
    validate_installations(root)
    if live:
        binary_set = binary_set_proof(root)
        schema_head = schema_head_proof(root)
        corpora = validate_corpora_live(root, SEPTEMBER_ROOT)
        proof = probe_live_environment(root)
        proof["binary_set"] = binary_set
        proof["schema_head"] = schema_head
        proof["corpora"] = corpora
        receipt = root / "receipts/runtime-verification.json"
        if not receipt.exists():
            write_once(receipt, json.dumps(proof, indent=2) + "\n", 0o600)
    validate_receipts(root)


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
