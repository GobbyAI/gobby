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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime_boundary import assert_owned_runtime, write_once

DEFAULT_RUNTIME_ROOT = Path("/Users/josh/Projects/wiki-bakeoff-code-2026-09")
DEFAULT_SOURCE_REPO = Path("/Users/josh/Projects/game-goblins")
DEFAULT_GOBBY_REPO = Path("/Users/josh/Projects/gobby")

BASE_SHA = "0216f1e33f05962d49467d95fe84609041c6dba8"
CHANGE_SHA = "8b24ac26699aac8b24254a647aa70b208287b492"
GOBBY_SHA = "7394b97c1d88c82f685e788e798de2cfd728ad15"
TASK_REF = "#21942"

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
P3_CASES = (
    "C0",
    "C1",
    "C2",
    "C3",
    "C7",
    "C8-baseline",
    "C8-change",
    "C9-baseline",
    "C9-change",
)
COMPARATOR_CASES = {
    "graphify": INDEX_CASES,
    "gcode": INDEX_CASES,
    "codewiki": P3_CASES,
    "opendeepwiki": P3_CASES,
    "grok-wiki": P3_CASES,
    "understand-anything": P3_CASES,
    "archify": P3_CASES,
}
CHANGED_CASES = frozenset({"C3", "C8-change", "C9-change"})

PORTS = {
    "postgres": 61234,
    "qdrant_http": 61235,
    "qdrant_grpc": 61236,
    "falkordb": 61237,
    "falkordb_browser": 61238,
    "daemon_http": 59480,
    "daemon_ws": 59481,
}
COMPOSE_PROJECT = "wiki-bakeoff-code-2026-09"
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
    return {
        "path": path.relative_to(root).as_posix(),
        "type": "symlink" if path.is_symlink() else "file",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
    }


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


def external_state_snapshot(source_repo: Path) -> dict[str, Any]:
    user_home = Path.home()
    gobby_home = user_home / ".gobby"
    codex_home = user_home / ".codex"
    stable_paths = [
        gobby_home / "bootstrap.yaml",
        gobby_home / ".secret_kek",
        gobby_home / ".secret_salt",
        gobby_home / "local_cli_token",
        gobby_home / "machine_id",
        gobby_home / "mcp-servers.json",
        gobby_home / "gcore.yaml",
        codex_home / "auth.json",
        codex_home / "config.toml",
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
        "stable_global_files": [_stable_file_record(path) for path in stable_paths],
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
    assert archive.stdout is not None
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
        "BAKEOFF_POSTGRES_DB=gobby_bakeoff_21942\n"
        "BAKEOFF_POSTGRES_USER=gobby_bakeoff_21942\n"
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
            "postgres_database": "gobby_bakeoff_21942",
            "postgres_user": "gobby_bakeoff_21942",
            "gobby_home": str(root / "gobby-home"),
            "gobby_source": str(root / "sources" / "gobby"),
            "gcode_binary": str(root / "tools" / "gcode" / "bin" / "gcode"),
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
    _write_json(
        root / "isolation" / "external-state.before.json",
        external_state_snapshot(source_repo),
    )

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
    before_copy = json.loads(
        (root / "isolation" / "external-state.before.json").read_text(encoding="utf-8")
    )["source"]
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
    _write_text(root / "config" / "compose.yaml", compose)
    image_path = root / "receipts" / "image-references.json"
    references = {"postgres": postgres_image, "qdrant": qdrant_image, "falkordb": falkordb_image}
    if image_path.exists():
        recorded = json.loads(image_path.read_text())
        assert all(recorded[name] == value for name, value in references.items())
    else:
        _write_json(image_path, references)


def snapshot_after(root: Path, source_repo: Path) -> None:
    _write_json(
        root / "isolation" / "external-state.after.json",
        external_state_snapshot(source_repo),
    )


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
    snapshot = subparsers.add_parser("snapshot-after")
    snapshot.add_argument("--owner-session", required=True)
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
    elif args.command == "snapshot-after":
        snapshot_after(root, args.source_repo.resolve())
    else:  # pragma: no cover - argparse enforces subcommands
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
