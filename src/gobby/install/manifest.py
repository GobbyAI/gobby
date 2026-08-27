"""Raw-byte manifest helpers for packaged bundled content."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import string
import subprocess
import sys
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict

MANIFEST_FILENAME = "bundled_content_manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_HASH_ALGORITHM = "sha256"
MANIFEST_ROOT = "shared"

_EXCLUDED_FILE_NAMES = {".DS_Store"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_EXCLUDED_DIR_NAMES = {"__pycache__"}
# Hash bundled files in modest chunks to avoid loading large artifacts at once.
_HASH_CHUNK_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = set(string.hexdigits)
_INSTALL_TREE_PATH = PurePosixPath("src/gobby/install")
_SHARED_TREE_PATH = _INSTALL_TREE_PATH / MANIFEST_ROOT
_MANIFEST_TREE_PATH = _INSTALL_TREE_PATH / MANIFEST_FILENAME


class BundledContentManifest(TypedDict):
    """Serialized raw-byte manifest schema."""

    schema_version: int
    hash_algorithm: str
    root: str
    files: dict[str, str]


@dataclass(frozen=True)
class CommittedManifestCheck:
    """Result of comparing committed shared blobs with the committed manifest."""

    ok: bool
    treeish: str
    errors: tuple[str, ...]
    expected_file_count: int


def hash_file_bytes(path: Path) -> str:
    """Return the SHA-256 hash of *path*'s raw bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        # iter(..., b"") reads until EOF while preserving binary bytes exactly.
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_include_bundled_file(path: Path, shared_dir: Path) -> bool:
    """Return whether *path* is a real bundled file for manifest purposes."""
    if path.is_symlink():
        return False
    if not path.is_file():
        return False
    try:
        relative = path.relative_to(shared_dir)
    except ValueError:
        return False
    return _should_include_relative_path(PurePosixPath(relative.as_posix()))


def iter_bundled_manifest_files(shared_dir: Path) -> list[Path]:
    """Return manifest-eligible files under *shared_dir* in deterministic order."""
    if not shared_dir.is_dir():
        return []
    files = [
        path for path in shared_dir.rglob("*") if should_include_bundled_file(path, shared_dir)
    ]
    return sorted(files, key=lambda path: path.relative_to(shared_dir).as_posix())


def build_bundled_content_manifest(shared_dir: Path) -> BundledContentManifest:
    """Build a deterministic raw-byte manifest for *shared_dir*."""
    files = {
        path.relative_to(shared_dir).as_posix(): hash_file_bytes(path)
        for path in iter_bundled_manifest_files(shared_dir)
    }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "hash_algorithm": MANIFEST_HASH_ALGORITHM,
        "root": MANIFEST_ROOT,
        "files": files,
    }


def write_bundled_content_manifest(install_dir: Path) -> Path:
    """Write the packaged bundled-content manifest below *install_dir*."""
    shared_dir = install_dir / MANIFEST_ROOT
    if not shared_dir.is_dir():
        raise FileNotFoundError(f"Shared directory not found: {shared_dir}")
    manifest_path = install_dir / MANIFEST_FILENAME
    manifest = build_bundled_content_manifest(shared_dir)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def load_bundled_content_manifest(install_dir: Path) -> tuple[dict[str, str] | None, list[str]]:
    """Load and validate the packaged bundled-content manifest."""
    manifest_path = install_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None, [f"Bundled content manifest not found: {manifest_path}"]

    try:
        raw_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, [f"Failed to read bundled content manifest: {exc}"]
    except json.JSONDecodeError as exc:
        return None, [f"Invalid bundled content manifest JSON: {exc}"]

    if not isinstance(raw_data, dict):
        return None, ["Bundled content manifest must be a JSON object"]

    errors: list[str] = []
    if raw_data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"Unsupported bundled content manifest schema_version: "
            f"{raw_data.get('schema_version')!r}"
        )
    if raw_data.get("hash_algorithm") != MANIFEST_HASH_ALGORITHM:
        errors.append(
            f"Unsupported bundled content manifest hash_algorithm: "
            f"{raw_data.get('hash_algorithm')!r}"
        )
    if raw_data.get("root") != MANIFEST_ROOT:
        errors.append(f"Unsupported bundled content manifest root: {raw_data.get('root')!r}")

    files_obj = raw_data.get("files")
    if not isinstance(files_obj, dict):
        errors.append("Bundled content manifest files must be an object")
        return None, errors

    files: dict[str, str] = {}
    for relative_path, digest in files_obj.items():
        if not isinstance(relative_path, str) or not _is_safe_manifest_path(relative_path):
            errors.append(f"Invalid bundled content manifest path: {relative_path!r}")
            continue
        if not isinstance(digest, str) or not _is_sha256_hexdigest(digest):
            errors.append(f"Invalid bundled content manifest hash for {relative_path!r}")
            continue
        files[relative_path] = digest.lower()

    if errors:
        return None, errors
    return files, []


def _is_safe_manifest_path(relative_path: str) -> bool:
    if not relative_path or "\\" in relative_path:
        return False
    parsed = PurePosixPath(relative_path)
    return (
        not parsed.is_absolute() and ".." not in parsed.parts and parsed.as_posix() == relative_path
    )


def _is_sha256_hexdigest(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(char in _HEX_DIGITS for char in value)


def check_committed_bundled_content_manifest(
    repo_root: Path,
    *,
    treeish: str = "HEAD",
) -> CommittedManifestCheck:
    """Compare manifest-eligible blobs and the manifest from one committed Git tree."""
    try:
        shared_files = _committed_shared_files(repo_root, treeish)
        expected: BundledContentManifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "hash_algorithm": MANIFEST_HASH_ALGORITHM,
            "root": MANIFEST_ROOT,
            "files": {
                relative: hashlib.sha256(content).hexdigest()
                for relative, content in shared_files.items()
            },
        }
        raw_manifest = _git_bytes(
            repo_root,
            "show",
            f"{treeish}:{_MANIFEST_TREE_PATH.as_posix()}",
        )
        committed = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return CommittedManifestCheck(
            ok=False,
            treeish=treeish,
            errors=(f"Cannot verify committed bundled content manifest: {_git_error_text(exc)}",),
            expected_file_count=0,
        )

    if committed == expected:
        return CommittedManifestCheck(True, treeish, (), len(expected["files"]))
    return CommittedManifestCheck(
        ok=False,
        treeish=treeish,
        errors=tuple(_manifest_parity_errors(committed, expected)),
        expected_file_count=len(expected["files"]),
    )


def check_linked_committed_bundled_manifest(
    repo_root: Path,
    commit_shas: Iterable[str],
) -> CommittedManifestCheck | None:
    """Check HEAD parity only when a prospective linked commit changes shared content."""
    for sha in commit_shas:
        try:
            changed = _git_bytes(
                repo_root,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                sha,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CommittedManifestCheck(
                ok=False,
                treeish="HEAD",
                errors=(f"Cannot inspect linked commit {sha}: {_git_error_text(exc)}",),
                expected_file_count=0,
            )
        if any(
            path.startswith(f"{_SHARED_TREE_PATH.as_posix()}/")
            for path in changed.decode("utf-8", errors="surrogateescape").split("\0")
            if path
        ):
            return check_committed_bundled_content_manifest(repo_root)
    return None


def _committed_shared_files(repo_root: Path, treeish: str) -> dict[str, bytes]:
    raw_archive = _git_bytes(
        repo_root,
        "archive",
        "--format=tar",
        treeish,
        _SHARED_TREE_PATH.as_posix(),
    )
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw_archive), mode="r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path == _SHARED_TREE_PATH:
                continue
            relative = path.relative_to(_SHARED_TREE_PATH)
            if not _should_include_relative_path(relative):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise OSError(f"Cannot read committed bundled file {member.name}")
            files[relative.as_posix()] = handle.read()
    return dict(sorted(files.items()))


def _should_include_relative_path(relative: PurePosixPath) -> bool:
    if any(part.startswith(".") for part in relative.parts):
        return False
    if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
        return False
    if relative.name in _EXCLUDED_FILE_NAMES:
        return False
    return relative.suffix not in _EXCLUDED_SUFFIXES


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


def _git_error_text(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            text = stderr.decode("utf-8", errors="replace").strip()
            if text:
                return text
        elif isinstance(stderr, str) and stderr.strip():
            return stderr.strip()
    return str(exc)


def _manifest_parity_errors(
    committed: object,
    expected: BundledContentManifest,
) -> list[str]:
    errors = ["Committed bundled content manifest is stale."]
    if not isinstance(committed, dict) or not isinstance(committed.get("files"), dict):
        return [*errors, "The committed manifest does not have the expected schema."]
    actual_files = committed["files"]
    expected_files = expected["files"]
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    changed = sorted(
        path
        for path in set(expected_files) & set(actual_files)
        if expected_files[path] != actual_files[path]
    )
    for label, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
        if paths:
            errors.append(f"{label.title()} entries: {', '.join(paths[:10])}")
    if not missing and not extra and not changed:
        errors.append("Manifest metadata differs from the committed schema.")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check committed bundled-content parity.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--treeish", default="HEAD")
    args = parser.parse_args(argv)
    result = check_committed_bundled_content_manifest(args.repo_root, treeish=args.treeish)
    if result.ok:
        print(
            f"Committed bundled content manifest matches {result.treeish} "
            f"({result.expected_file_count} files)."
        )
        return 0
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
