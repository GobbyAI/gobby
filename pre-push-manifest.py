#!/usr/bin/env python3
"""Maintain the integrity manifest for a pre-push report run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
INVALIDATED_EXIT_CODE = 2
_REPORT_PATHSPECS = (".", ":(exclude)reports", ":(exclude)reports/**")


class ManifestError(RuntimeError):
    """Raised when a report-run manifest cannot be updated safely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", *args),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        raise ManifestError(f"git {' '.join(args)} failed: {error}")
    return result.stdout


def _head(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "--verify", "HEAD").decode().strip()


def worktree_fingerprint(repo_root: Path) -> str:
    """Hash index state plus content for every dirty or untracked non-report path."""
    digest = hashlib.sha256(b"gobby-pre-push-worktree-v1\0")
    pathspecs = ("--", *_REPORT_PATHSPECS)
    digest.update(_git(repo_root, "ls-files", "--stage", "-z", *pathspecs))
    digest.update(
        _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            *pathspecs,
        )
    )
    dirty_paths = _git(
        repo_root,
        "ls-files",
        "--modified",
        "--others",
        "--exclude-standard",
        "-z",
        *pathspecs,
    ).split(b"\0")
    for raw_path in sorted(path for path in dirty_paths if path):
        digest.update(raw_path)
        digest.update(b"\0")
        path = repo_root / os.fsdecode(raw_path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        digest.update(
            f"{stat.S_IFMT(metadata.st_mode):o}:{stat.S_IMODE(metadata.st_mode):o}\0".encode()
        )
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _load(manifest_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read {manifest_path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"{manifest_path} is not a supported pre-push manifest")
    return value


def _write(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            delete=False,
        ) as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(manifest_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _repo_root(manifest: dict[str, Any]) -> Path:
    value = manifest.get("repo_root")
    if not isinstance(value, str) or not value:
        raise ManifestError("manifest is missing repo_root")
    return Path(value)


def _integrity_state(manifest: dict[str, Any]) -> tuple[str, str, list[str]]:
    repo_root = _repo_root(manifest)
    ending_commit = _head(repo_root)
    ending_fingerprint = worktree_fingerprint(repo_root)
    reasons: list[str] = []
    if ending_commit != manifest.get("starting_commit"):
        reasons.append("HEAD changed during the report run")
    if ending_fingerprint != manifest.get("starting_worktree_fingerprint"):
        reasons.append("tracked or untracked source changed during the report run")
    return ending_commit, ending_fingerprint, reasons


def _complete(
    manifest: dict[str, Any],
    *,
    requested_status: str,
) -> tuple[dict[str, Any], bool]:
    ending_commit, ending_fingerprint, reasons = _integrity_state(manifest)
    invalidated = bool(reasons)
    manifest.update(
        {
            "status": "invalidated" if invalidated else requested_status,
            "completed_at": _now(),
            "ending_commit": ending_commit,
            "ending_worktree_fingerprint": ending_fingerprint,
            "invalidation_reasons": reasons,
        }
    )
    return manifest, invalidated


def start_manifest(manifest_path: Path, repo_root: Path) -> None:
    resolved_root = repo_root.resolve()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "repo_root": str(resolved_root),
        "started_at": _now(),
        "completed_at": None,
        "starting_commit": _head(resolved_root),
        "ending_commit": None,
        "starting_worktree_fingerprint": worktree_fingerprint(resolved_root),
        "ending_worktree_fingerprint": None,
        "commands": [],
        "status": "running",
        "invalidation_reasons": [],
    }
    _write(manifest_path, manifest)


def record_command(
    manifest_path: Path,
    *,
    name: str,
    report: str | None,
    exit_code: int | None,
    status_value: str | None,
    gating: bool,
) -> bool:
    manifest = _load(manifest_path)
    if manifest.get("status") != "running":
        raise ManifestError(f"cannot record a command on a {manifest.get('status')} manifest")
    status_name = status_value or ("passed" if exit_code == 0 else "failed")
    manifest["commands"].append(
        {
            "name": name,
            "report": report,
            "exit_code": exit_code,
            "status": status_name,
            "gating": gating,
            "completed_at": _now(),
        }
    )
    ending_commit, ending_fingerprint, reasons = _integrity_state(manifest)
    invalidated = bool(reasons)
    if invalidated:
        manifest.update(
            {
                "status": "invalidated",
                "completed_at": _now(),
                "ending_commit": ending_commit,
                "ending_worktree_fingerprint": ending_fingerprint,
                "invalidation_reasons": reasons,
            }
        )
    _write(manifest_path, manifest)
    return invalidated


def finish_manifest(manifest_path: Path, requested_status: str) -> bool:
    manifest = _load(manifest_path)
    if manifest.get("status") != "running":
        return manifest.get("status") == "invalidated"
    manifest, invalidated = _complete(manifest, requested_status=requested_status)
    _write(manifest_path, manifest)
    return invalidated


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--manifest", type=Path, required=True)
    start.add_argument("--repo-root", type=Path, required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--manifest", type=Path, required=True)
    record.add_argument("--name", required=True)
    record.add_argument("--report")
    record.add_argument("--exit-code", type=int)
    record.add_argument("--status", choices=("passed", "failed", "skipped"))
    record.add_argument("--non-gating", action="store_true")

    finish = subparsers.add_parser("finish")
    finish.add_argument("--manifest", type=Path, required=True)
    finish.add_argument("--status", choices=("passed", "failed"), required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "start":
            start_manifest(args.manifest, args.repo_root)
            return 0
        if args.command == "record":
            if args.status is None and args.exit_code is None:
                raise ManifestError("record requires --exit-code or --status")
            invalidated = record_command(
                args.manifest,
                name=args.name,
                report=args.report,
                exit_code=args.exit_code,
                status_value=args.status,
                gating=not args.non_gating,
            )
        else:
            invalidated = finish_manifest(args.manifest, args.status)
    except ManifestError as exc:
        print(f"pre-push manifest error: {exc}", file=sys.stderr)
        return 1
    return INVALIDATED_EXIT_CODE if invalidated else 0


if __name__ == "__main__":
    raise SystemExit(main())
