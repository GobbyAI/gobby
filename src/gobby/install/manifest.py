"""Raw-byte manifest helpers for packaged bundled content."""

from __future__ import annotations

import hashlib
import json
import string
from pathlib import Path, PurePosixPath
from typing import TypedDict

MANIFEST_FILENAME = "bundled_content_manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_HASH_ALGORITHM = "sha256"
MANIFEST_ROOT = "shared"

_EXCLUDED_FILE_NAMES = {".DS_Store"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_EXCLUDED_DIR_NAMES = {"__pycache__"}
_HASH_CHUNK_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = set(string.hexdigits)


class BundledContentManifest(TypedDict):
    """Serialized raw-byte manifest schema."""

    schema_version: int
    hash_algorithm: str
    root: str
    files: dict[str, str]


def hash_file_bytes(path: Path) -> str:
    """Return the SHA-256 hash of *path*'s raw bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_include_bundled_file(path: Path, shared_dir: Path) -> bool:
    """Return whether *path* is a real bundled file for manifest purposes."""
    if not path.is_file():
        return False
    try:
        relative = path.relative_to(shared_dir)
    except ValueError:
        return False
    if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
        return False
    if path.name in _EXCLUDED_FILE_NAMES:
        return False
    return path.suffix not in _EXCLUDED_SUFFIXES


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
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def load_bundled_content_manifest(install_dir: Path) -> tuple[dict[str, str] | None, list[str]]:
    """Load and validate the packaged bundled-content manifest."""
    manifest_path = install_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None, [f"Bundled content manifest not found: {manifest_path}"]

    try:
        raw_data = json.loads(manifest_path.read_text(encoding="utf-8"))
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
