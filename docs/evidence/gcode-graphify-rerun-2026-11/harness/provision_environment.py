"""Provision reproducible inputs and configuration for the code-wiki bakeoff.

This script never starts or stops services. It creates a new runtime root from
frozen Git objects, or performs narrow resumable operations against that root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime_boundary import (
    EvidenceError,
    assert_owned_runtime,
    file_record,
    require,
    symlink_record,
    write_once,
)
from schema_head import gcode_binary_relative

DEFAULT_RUNTIME_ROOT = Path(
    "/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf"
    "/epics/epic-22948/build/gcode-graphify-rerun-2026-11"
)
DEFAULT_SOURCE_REPO = Path("/Users/josh/Projects/game-goblins")
DEFAULT_GOBBY_REPO = Path("/Users/josh/Projects/gobby")

BASE_SHA = "0216f1e33f05962d49467d95fe84609041c6dba8"
CHANGE_SHA = "8b24ac26699aac8b24254a647aa70b208287b492"
GOBBY_SHA = "1a13024ceda47fb25bebefc8b42ee9d2e372778d"
TASK_REF = "#22948"
PROBE_CRATE = "gobby-core"
PROBE_NAME = "managed_bearer_probe"
PROBE_SOURCE = f"crates/gcore/examples/{PROBE_NAME}.rs"
PROBE_BINARY = f"probes/{PROBE_NAME}"
PROBE_MANIFEST = "manifests/bearer-probe.json"
PROBE_TARGET = "build/probe-target"

SOURCE_EXCLUSIONS = (
    ".gobby/project.json",
    ".gobby/mcp/servers/lightspeed.yaml",
)
CONTROL_EXCLUSIONS = (
    ".gobby/project.json",
    ".gobby/gcode.json",
    ".gobby/isolation.json",
)
INDEX_CASES = (
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
    "C8-baseline",
    "C8-change",
    "C9-baseline",
    "C9-change",
)
COMPARATOR_CASES = {
    "graphify": INDEX_CASES,
    "gcode": INDEX_CASES,
}
CHANGED_CASES = frozenset({"C3", "C8-change", "C9-change"})
PROJECT_MANIFEST = "config/project-manifest.json"
C0_PROJECT = "gcode-C0-probe"
# The C0 probe's registered workspace: the only project path C0, the grant and --live read.
C0_WORKSPACE = f"workspaces/{C0_PROJECT}"
SCORED_CASES = ("C1", "C2", "C3", "C4", "C5", "C6")
SINGLE_PROJECTS = (
    ("gcode-C7-interrupted", "C7", "interrupted"),
    ("gcode-C7-clean", "C7", "clean"),
    ("gcode-C8-baseline", "C8-baseline", "baseline"),
    ("gcode-C8-change", "C8-change", "change"),
    ("gcode-C1-labels-properties", "C1", "labels-properties"),
    (C0_PROJECT, "C0", "probe"),
)

PORTS = {
    "postgres": 61234,
    "qdrant_http": 61235,
    "qdrant_grpc": 61236,
    "falkordb": 61237,
    "falkordb_browser": 61238,
    "daemon_http": 59480,
    "daemon_ws": 59481,
}
COMPOSE_PROJECT = "gcode-graphify-rerun-2026-11"
CONTAINERS = {
    "postgres": f"{COMPOSE_PROJECT}-postgres",
    "qdrant": f"{COMPOSE_PROJECT}-qdrant",
    "falkordb": f"{COMPOSE_PROJECT}-falkordb",
}
VOLUMES = {
    "postgres": f"{COMPOSE_PROJECT}-postgres-data",
    "qdrant": f"{COMPOSE_PROJECT}-qdrant-data",
    "falkordb": f"{COMPOSE_PROJECT}-falkordb-data",
}
NETWORK = f"{COMPOSE_PROJECT}-network"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, check=check, capture_output=True)


def _write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    write_once(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def _write_text(path: Path, value: str, *, mode: int = 0o644) -> None:
    write_once(path, value, mode)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode("utf-8")
    return path.read_bytes()


def _path_record(path: Path, root: Path) -> dict[str, Any]:
    metadata = path.lstat()
    payload = _path_bytes(path)
    record: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "type": "symlink" if path.is_symlink() else "file",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
    }
    if path.is_symlink():
        record.update(symlink_record(root, path))
    return record


def build_manifest(root: Path, commit: str, exclusions: list[dict[str, str]]) -> dict[str, Any]:
    def is_input_file(path: Path) -> bool:
        relative = path.relative_to(root).as_posix()
        return relative not in CONTROL_EXCLUSIONS and (path.is_file() or path.is_symlink())

    files = [_path_record(path, root) for path in sorted(root.rglob("*")) if is_input_file(path)]
    tree = hashlib.sha256()
    for record in files:
        path = root / record["path"]
        payload = _path_bytes(path)
        for field in (
            record["path"].encode("utf-8"),
            record["mode"].encode("ascii"),
            str(record["bytes"]).encode("ascii"),
        ):
            tree.update(field)
            tree.update(b"\0")
        tree.update(payload)
    return {
        "schema_version": 1,
        "source_commit": commit,
        "root": str(root),
        "control_exclusions": list(CONTROL_EXCLUSIONS),
        "source_exclusions": exclusions,
        "file_count": len(files),
        "total_bytes": sum(record["bytes"] for record in files),
        "input_tree_sha256": tree.hexdigest(),
        "files": files,
    }


def _git_status(repo: Path) -> dict[str, Any]:
    status = _run(["git", "status", "--porcelain=v1", "-uall"], cwd=repo).stdout.decode()
    head = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.decode().strip()
    index = repo / ".git" / "index"
    return {
        "repo": str(repo.resolve()),
        "head": head,
        "status_porcelain_v1": status,
        "status_sha256": _sha256_bytes(status.encode()),
        "index_sha256": _sha256_file(index) if index.is_file() else None,
    }


def _stable_file_record(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "exists": False}
    metadata = path.lstat()
    if not path.is_file() and not path.is_symlink():
        return {
            "path": str(path),
            "exists": True,
            "type": "directory" if path.is_dir() else "other",
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        }
    payload = _path_bytes(path)
    return {
        "path": str(path),
        "exists": True,
        "type": "symlink" if path.is_symlink() else "file",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
    }


def _command_observation(argv: list[str]) -> dict[str, Any]:
    try:
        completed = _run(argv, check=False)
    except OSError as error:
        return {"argv": argv, "exit_code": None, "error": str(error)}
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }


def _operator_stat_record(path: Path) -> dict[str, Any]:
    """Record an operator file by stat only. Its bytes are never opened or hashed."""
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "exists": False}
    metadata = path.lstat()
    if path.is_symlink():
        kind = "symlink"
    elif path.is_file():
        kind = "file"
    elif path.is_dir():
        kind = "directory"
    else:
        kind = "other"
    return {
        "path": str(path),
        "exists": True,
        "type": kind,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "inode": metadata.st_ino,
    }


def external_state_snapshot(source_repo: Path) -> dict[str, Any]:
    user_home = Path.home()
    gobby_home = user_home / ".gobby"
    codex_home = user_home / ".codex"
    # Operator-owned files: the operator's standing rule forbids reading their bytes. Codex
    # rewrites config.toml, so it is fingerprinted by stat (inode, size, mtime_ns, mode) too.
    operator_paths = [
        gobby_home / "bootstrap.yaml",
        gobby_home / ".secret_kek",
        gobby_home / ".secret_salt",
        gobby_home / "local_cli_token",
        codex_home / "auth.json",
        codex_home / "config.toml",
    ]
    stable_paths = [
        gobby_home / "machine_id",
        gobby_home / "mcp-servers.json",
        gobby_home / "gcore.yaml",
        codex_home / "hooks.json",
        codex_home / "installation_id",
        codex_home / "version.json",
        user_home / ".claude" / "settings.json",
    ]
    for name in (
        "gclient",
        "gcode",
        "gdaemon",
        "ghook",
        "gterm",
        ".gcode-install.json",
        ".gcode-version",
        ".gdaemon-install.json",
        ".gdaemon-version",
    ):
        stable_paths.append(gobby_home / "bin" / name)
    ambient_names = (
        "GOBBY_AGENT_RUN_ID",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_SESSION_ID",
        "GOBBY_PARENT_SESSION_ID",
        "GOBBY_HOME",
        "CODEX_HOME",
        "DATABASE_URL",
    )
    return {
        "captured_at": _utc_now(),
        "platform": platform.platform(),
        "source": _git_status(source_repo),
        "stable_global_files": [
            *(_stable_file_record(path) for path in stable_paths),
            *(_operator_stat_record(path) for path in operator_paths),
        ],
        "ambient_environment_presence": {
            name: name in os.environ and bool(os.environ[name]) for name in ambient_names
        },
        "docker_containers": _command_observation(
            ["docker", "ps", "--no-trunc", "--format", "{{json .}}"]
        ),
        "docker_volumes": _command_observation(["docker", "volume", "ls", "--format", "{{.Name}}"]),
        "shared_port_listeners": _command_observation(
            [
                "lsof",
                "-nP",
                "-iTCP:60891",
                "-iTCP:60892",
                "-iTCP:6333",
                "-iTCP:6334",
                "-iTCP:16379",
                "-iTCP:13000",
                "-sTCP:LISTEN",
            ]
        ),
    }


def _archive(repo: Path, commit: str, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    destination.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", commit],
        cwd=repo,
        stdout=subprocess.PIPE,
    )
    require(archive.stdout is not None, "check failed: archive.stdout is not None")
    extracted = subprocess.run(["tar", "-x", "-C", str(destination)], stdin=archive.stdout)
    archive.stdout.close()
    archive_status = archive.wait()
    if archive_status != 0 or extracted.returncode != 0:
        raise RuntimeError(
            f"archive failed for {repo}@{commit}: git={archive_status} tar={extracted.returncode}"
        )


def _remove_source_exclusions(template: Path) -> list[dict[str, str]]:
    records = []
    for relative in SOURCE_EXCLUSIONS:
        target = template / relative
        if not target.is_file():
            raise FileNotFoundError(f"required exclusion is absent: {target}")
        records.append(
            {
                "path": relative,
                "reason": "operational Gobby identity or live-service connection",
                "source_sha256": _sha256_file(target),
            }
        )
        target.unlink()
    return records


def _write_runtime_secrets(root: Path) -> None:
    postgres_password = secrets.token_urlsafe(32)
    falkordb_password = secrets.token_urlsafe(32)
    env_text = (
        "BAKEOFF_POSTGRES_DB=gobby_bakeoff_22948\n"
        "BAKEOFF_POSTGRES_USER=gobby_bakeoff_22948\n"
        f"BAKEOFF_POSTGRES_PASSWORD={postgres_password}\n"
        f"BAKEOFF_FALKORDB_PASSWORD={falkordb_password}\n"
    )
    _write_text(root / "config" / "services.env", env_text, mode=0o600)
    _write_json(
        root / "config" / "runtime.json",
        {
            "schema_version": 1,
            "compose_project": COMPOSE_PROJECT,
            "containers": CONTAINERS,
            "volumes": VOLUMES,
            "network": NETWORK,
            "ports": PORTS,
            "bind_address": "127.0.0.1",
            "postgres_database": "gobby_bakeoff_22948",
            "postgres_user": "gobby_bakeoff_22948",
            "gobby_home": str(root / "gobby-home"),
            "gobby_source": str(root / "sources" / "gobby"),
            "gcode_binary": str(root / gcode_binary_relative()),
        },
    )


def init_runtime(root: Path, source_repo: Path, gobby_repo: Path, owner_session: str) -> None:
    if not owner_session.strip():
        raise ValueError("an explicit owner session is required")
    if root.exists():
        raise FileExistsError(f"refusing to overwrite existing runtime root: {root}")
    root.mkdir(parents=True, mode=0o700)
    for relative in (
        "build",
        "config",
        "corpora",
        "gobby-home/files",
        "gobby-home/logs",
        "isolation",
        "logs/install",
        "manifests/corpora",
        "manifests/templates",
        "receipts",
        "results",
        "sources",
        "state",
        "tools",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)

    _write_json(
        root / "ownership.json",
        {
            "schema_version": 1,
            "created_at": _utc_now(),
            "owner_task": TASK_REF,
            "owner_session": owner_session,
            "runtime_root": str(root),
            "owned_compose_project": COMPOSE_PROJECT,
            "owned_containers": sorted(CONTAINERS.values()),
            "owned_volumes": sorted(VOLUMES.values()),
            "owned_network": NETWORK,
            "owned_daemon_pid_file": str(root / "gobby-home" / "gobby.pid"),
            "teardown_boundary": "stop only IDs recorded under receipts/ownership-live.json",
        },
    )
    before_copy = _git_status(source_repo)

    gobby_source = root / "sources" / "gobby"
    _archive(gobby_repo, GOBBY_SHA, gobby_source)
    _write_json(
        root / "manifests" / "gobby-source.json",
        build_manifest(gobby_source, GOBBY_SHA, []),
    )

    exclusions_by_template: dict[str, list[dict[str, str]]] = {}
    for name, commit in (("baseline", BASE_SHA), ("change", CHANGE_SHA)):
        destination = root / "templates" / name
        _archive(source_repo, commit, destination)
        exclusions = _remove_source_exclusions(destination)
        exclusions_by_template[name] = exclusions
        _write_json(
            root / "manifests" / "templates" / f"{name}.json",
            build_manifest(destination, commit, exclusions),
        )

    for comparator, cases in COMPARATOR_CASES.items():
        for case in cases:
            template_name = "change" if case in CHANGED_CASES else "baseline"
            commit = CHANGE_SHA if template_name == "change" else BASE_SHA
            source = root / "templates" / template_name
            destination = root / "corpora" / comparator / case
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"refusing to overwrite {destination}")
            shutil.copytree(source, destination, symlinks=True)
            _write_json(
                root / "manifests" / "corpora" / comparator / f"{case}.json",
                build_manifest(destination, commit, exclusions_by_template[template_name]),
            )

    after_copy = _git_status(source_repo)
    if after_copy != before_copy:
        raise RuntimeError("source checkout changed while creating Git-object archives")
    _write_json(root / "isolation" / "source-status.after-corpus.json", after_copy)
    _write_runtime_secrets(root)
    _write_json(
        root / "receipts" / "init.json",
        {
            "completed_at": _utc_now(),
            "base_commit": BASE_SHA,
            "change_commit": CHANGE_SHA,
            "gobby_commit": GOBBY_SHA,
            "comparator_count": len(COMPARATOR_CASES),
            "corpus_copy_count": sum(len(cases) for cases in COMPARATOR_CASES.values()),
        },
    )


def refresh_manifest(root: Path, comparator: str, case: str) -> None:
    if comparator not in COMPARATOR_CASES or case not in COMPARATOR_CASES[comparator]:
        raise ValueError("unknown comparator case")
    corpus = root / "corpora" / comparator / case
    manifest_path = root / "manifests" / "corpora" / comparator / f"{case}.json"
    if not corpus.is_dir() or not manifest_path.is_file():
        raise FileNotFoundError(f"unknown corpus {comparator}/{case}")
    old = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed = build_manifest(corpus, old["source_commit"], old["source_exclusions"])
    _write_json(
        root / "observations" / comparator / case / f"{observed['input_tree_sha256']}.json",
        observed,
    )


def project_entries() -> list[dict[str, Any]]:
    """The 54 criterion 2.1.3 projects: 6 repetitions and 2 spares per scored case, plus singles.

    Spares take the repetition slots 7 and 8, so each case owns eight slots, six scored.
    """
    entries: list[dict[str, Any]] = []
    for case in SCORED_CASES:
        for repetition in range(1, 7):
            entries.append(
                _project_entry(f"gcode-{case}-r{repetition}", case, "repetition", repetition)
            )
        for spare in (1, 2):
            entries.append(_project_entry(f"gcode-{case}-spare{spare}", case, "spare", 6 + spare))
    for name, case, role in SINGLE_PROJECTS:
        entries.append(_project_entry(name, case, role, 1))
    return entries


def _project_entry(name: str, case: str, role: str, repetition: int) -> dict[str, Any]:
    return {
        "name": name,
        "case": case,
        "role": role,
        "repetition": repetition,
        "corpus": f"corpora/gcode/{case}",
        "workspace": f"workspaces/{name}",
    }


def write_project_manifest(root: Path) -> dict[str, Any]:
    entries = project_entries()
    for entry in entries:
        require((root / entry["corpus"]).is_dir(), f"missing sealed corpus {entry['corpus']}")
    manifest = {"count": len(entries), "projects": entries}
    _write_json(root / PROJECT_MANIFEST, manifest)
    return manifest


def read_project_manifest(root: Path) -> list[dict[str, Any]]:
    path = root / PROJECT_MANIFEST
    require(path.is_file(), f"missing project manifest {PROJECT_MANIFEST}")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    expected = project_entries()
    require(
        recorded.get("count") == len(expected) and recorded.get("projects") == expected,
        "project manifest is not the criterion 2.1.3 set",
    )
    return expected


def materialize_workspaces(root: Path) -> None:
    pairs = [
        (root / project["corpus"], root / project["workspace"])
        for project in read_project_manifest(root)
    ]
    for _, workspace in pairs:
        require(
            not workspace.exists() and not workspace.is_symlink(),
            f"refusing to overwrite workspace {workspace.relative_to(root)}",
        )
    for corpus, workspace in pairs:
        shutil.copytree(corpus, workspace, symlinks=True)


def write_compose(
    root: Path,
    postgres_image: str,
    qdrant_image: str,
    falkordb_image: str,
) -> None:
    for name, image in (
        ("postgres", postgres_image),
        ("qdrant", qdrant_image),
        ("falkordb", falkordb_image),
    ):
        if ":latest" in image:
            raise ValueError(f"{name} image must not use the :latest tag")
        if re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image) is None:
            raise ValueError(f"{name} image must be a resolved immutable reference")
    compose = f"""name: {COMPOSE_PROJECT}
services:
  postgres:
    image: {postgres_image}
    container_name: {CONTAINERS["postgres"]}
    command: [postgres, -c, "shared_preload_libraries=pg_search,pgaudit", -c, pgaudit.log=none]
    environment:
      POSTGRES_DB: ${{BAKEOFF_POSTGRES_DB}}
      POSTGRES_USER: ${{BAKEOFF_POSTGRES_USER}}
      POSTGRES_PASSWORD: ${{BAKEOFF_POSTGRES_PASSWORD}}
    ports:
      - "127.0.0.1:{PORTS["postgres"]}:5432"
    volumes:
      - postgres-data:/var/lib/postgresql
    healthcheck:
      test: [CMD-SHELL, 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"']
      interval: 2s
      timeout: 2s
      retries: 30
  qdrant:
    image: {qdrant_image}
    container_name: {CONTAINERS["qdrant"]}
    environment:
      QDRANT__LOG_LEVEL: WARN
    ports:
      - "127.0.0.1:{PORTS["qdrant_http"]}:6333"
      - "127.0.0.1:{PORTS["qdrant_grpc"]}:6334"
    volumes:
      - qdrant-data:/qdrant/storage
    healthcheck:
      test: [CMD-SHELL, 'bash -c ''exec 3<>/dev/tcp/localhost/6333 && printf "GET /healthz HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n" >&3 && grep -q "healthz check passed" <&3''']
      interval: 2s
      timeout: 2s
      retries: 30
  falkordb:
    image: {falkordb_image}
    container_name: {CONTAINERS["falkordb"]}
    environment:
      REDIS_ARGS: --requirepass ${{BAKEOFF_FALKORDB_PASSWORD}} --save 3600 1 300 100
      BAKEOFF_FALKORDB_PASSWORD: ${{BAKEOFF_FALKORDB_PASSWORD}}
      FALKORDB_ARGS: MAX_QUEUED_QUERIES 25 TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0 RESULTSET_SIZE 10000
    ports:
      - "127.0.0.1:{PORTS["falkordb"]}:6379"
      - "127.0.0.1:{PORTS["falkordb_browser"]}:3000"
    volumes:
      - falkordb-data:/var/lib/falkordb/data
    healthcheck:
      test: [CMD-SHELL, 'redis-cli -a "$$BAKEOFF_FALKORDB_PASSWORD" PING | grep -q PONG']
      interval: 2s
      timeout: 2s
      retries: 30
volumes:
  postgres-data:
    name: {VOLUMES["postgres"]}
  qdrant-data:
    name: {VOLUMES["qdrant"]}
  falkordb-data:
    name: {VOLUMES["falkordb"]}
networks:
  default:
    name: {NETWORK}
"""
    require(":latest" not in compose, "rendered compose names a :latest image")
    _write_text(root / "config" / "compose.yaml", compose)
    image_path = root / "receipts" / "image-references.json"
    references = {"postgres": postgres_image, "qdrant": qdrant_image, "falkordb": falkordb_image}
    if image_path.exists():
        recorded = json.loads(image_path.read_text())
        require(
            all(recorded[name] == value for name, value in references.items()),
            "check failed: all((recorded[name] == value for name, value in references.items()))",
        )
    else:
        _write_json(image_path, references)


ImageRunner = Callable[[list[str]], subprocess.CompletedProcess[bytes]]


def _docker_command(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, check=False)


def ensure_images(images: Mapping[str, str], run: ImageRunner) -> None:
    """Pull each pinned reference the local daemon lacks; stop at the first failed pull."""
    for name, reference in images.items():
        if run(["docker", "image", "inspect", reference]).returncode == 0:
            continue
        pulled = run(["docker", "pull", reference])
        if pulled.returncode != 0:
            detail = pulled.stderr.decode("utf-8", errors="replace").strip()
            raise EvidenceError(f"docker pull failed for {name} image {reference}: {detail}")


CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[bytes]]
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _command_run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, check=False, capture_output=True)


def _stderr_text(done: subprocess.CompletedProcess[bytes]) -> str:
    return done.stderr.decode("utf-8", errors="replace").strip()


def _candidate_blob(run: CommandRunner, gobby_repo: Path, candidate: str) -> bytes:
    """Read-only git checks that the candidate changes exactly the example; returns its blob."""
    crates = run(["git", "diff", "--name-only", GOBBY_SHA, candidate, "--", "crates"], gobby_repo)
    require(crates.returncode == 0, f"git could not compare crates: {_stderr_text(crates)}")
    changed = crates.stdout.decode("utf-8").splitlines()
    require(
        changed == [PROBE_SOURCE],
        f"candidate must change exactly {PROBE_SOURCE} under crates, not {changed}",
    )
    lock = run(["git", "diff", "--quiet", GOBBY_SHA, candidate, "--", "Cargo.lock"], gobby_repo)
    require(
        lock.returncode == 0,
        f"Cargo.lock differs from the pin or cannot be compared (git exit {lock.returncode})",
    )
    blob = run(["git", "show", f"{candidate}:{PROBE_SOURCE}"], gobby_repo)
    require(blob.returncode == 0, f"git could not read {PROBE_SOURCE}: {_stderr_text(blob)}")
    return blob.stdout


def build_probe(
    root: Path, probe_source: Path, candidate: str, gobby_repo: Path, run: CommandRunner
) -> dict[str, Any]:
    """Build the bearer probe from the pinned tree plus its one recorded example overlay.

    The pin predates the example, and gobby-core is identical between the pin and the candidate,
    so the overlay is the only source delta. The overlay must be the candidate's own blob, and the
    candidate must change nothing else under crates or in Cargo.lock. Cargo writes only under the
    runtime root's target dir.
    """
    archived = root / "sources" / "gobby" / PROBE_SOURCE
    binary = root / PROBE_BINARY
    require(not archived.exists(), f"the pinned tree already contains {PROBE_SOURCE}")
    require(not binary.exists(), f"refusing to overwrite {binary}")
    require(
        probe_source.is_file() and not probe_source.is_symlink(),
        f"probe source is not a plain file: {probe_source}",
    )
    require(FULL_SHA.fullmatch(candidate) is not None, "candidate must be a full commit SHA")
    probe_bytes = probe_source.read_bytes()
    blob = _candidate_blob(run, gobby_repo, candidate)
    require(
        _sha256_bytes(blob) == _sha256_bytes(probe_bytes),
        f"probe source differs from the candidate blob {PROBE_SOURCE}@{candidate}",
    )
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_bytes(probe_bytes)
    argv = [
        "cargo",
        "build",
        "--locked",
        "--target-dir",
        str(root / PROBE_TARGET),
        "-p",
        PROBE_CRATE,
        "--example",
        PROBE_NAME,
    ]
    built = run(argv, root / "sources" / "gobby")
    if built.returncode != 0:
        detail = built.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceError(f"cargo build failed for {PROBE_NAME}: {detail}")
    artifact = root / PROBE_TARGET / "debug" / "examples" / PROBE_NAME
    require(artifact.is_file(), f"cargo built no artifact at {artifact}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(artifact.read_bytes())
    binary.chmod(0o700)
    example_source = file_record(root, archived)
    manifest = {
        "pinned_commit": GOBBY_SHA,
        "example_in_pin": False,
        "candidate_commit": candidate,
        "candidate_blob_sha256": _sha256_bytes(blob),
        "archived_copy_sha256": example_source["sha256"],
        "example_source": example_source,
        "binary": file_record(root, binary),
        "cargo_argv": argv,
    }
    _write_json(root / PROBE_MANIFEST, manifest)
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--source-repo", type=Path, default=DEFAULT_SOURCE_REPO)
    parser.add_argument("--gobby-repo", type=Path, default=DEFAULT_GOBBY_REPO)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--owner-session", required=True)
    refresh = subparsers.add_parser("refresh-manifest")
    refresh.add_argument("--owner-session", required=True)
    refresh.add_argument("--comparator", choices=sorted(COMPARATOR_CASES), required=True)
    refresh.add_argument("--case", required=True)
    compose = subparsers.add_parser("write-compose")
    compose.add_argument("--owner-session", required=True)
    compose.add_argument("--postgres-image", required=True)
    compose.add_argument("--qdrant-image", required=True)
    compose.add_argument("--falkordb-image", required=True)
    probe = subparsers.add_parser("build-probe")
    probe.add_argument("--owner-session", required=True)
    probe.add_argument("--probe-source", type=Path, required=True)
    probe.add_argument("--candidate", required=True)
    manifest = subparsers.add_parser("write-manifest")
    manifest.add_argument("--owner-session", required=True)
    materialize = subparsers.add_parser("materialize-workspaces")
    materialize.add_argument("--owner-session", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = args.runtime_root.resolve()
    if root != DEFAULT_RUNTIME_ROOT:
        raise ValueError(f"runtime root must be {DEFAULT_RUNTIME_ROOT}")
    if args.command != "init":
        assert_owned_runtime(root, args.owner_session)
    if args.command == "init":
        init_runtime(
            root, args.source_repo.resolve(), args.gobby_repo.resolve(), args.owner_session
        )
    elif args.command == "refresh-manifest":
        refresh_manifest(root, args.comparator, args.case)
    elif args.command == "write-compose":
        write_compose(root, args.postgres_image, args.qdrant_image, args.falkordb_image)
        ensure_images(
            {
                "postgres": args.postgres_image,
                "qdrant": args.qdrant_image,
                "falkordb": args.falkordb_image,
            },
            _docker_command,
        )
    elif args.command == "build-probe":
        build_probe(
            root,
            args.probe_source.resolve(),
            args.candidate,
            args.gobby_repo.resolve(),
            _command_run,
        )
    elif args.command == "write-manifest":
        write_project_manifest(root)
    elif args.command == "materialize-workspaces":
        materialize_workspaces(root)
    else:  # pragma: no cover - argparse enforces subcommands
        raise EvidenceError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
