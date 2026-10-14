"""Version and source provenance for workspace binary releases."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from gobby.install.version_probe import probe_native_bin_version
from gobby.utils import spawn
from gobby.utils.native_bin import native_bin_name

RELEASE_CRATES = {
    "gcode": "gcode",
    "gdaemon": "gdaemon",
    "ghook": "ghook",
    "gclient": "gclient",
    "gterm": "gterminal",
}


class BinaryReleaseVersionError(RuntimeError):
    """A promotion would conceal source changes under an installed version."""


@dataclass(frozen=True)
class BinaryReleaseIdentity:
    version: str
    source_identity: str
    artifact_sha256: str


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _crate_closure(root: Path, crate: Path) -> set[Path]:
    """Include normal, build, target and workspace-inherited path dependencies."""
    workspace_dependencies = _toml(root / "Cargo.toml").get("workspace", {}).get("dependencies", {})
    pending = [crate.resolve()]
    found: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in found:
            continue
        current.relative_to(root.resolve())
        found.add(current)
        manifest = _toml(current / "Cargo.toml")
        tables = [manifest, *manifest.get("target", {}).values()]
        for table in tables:
            for kind in ("dependencies", "build-dependencies"):
                for name, dependency in table.get(kind, {}).items():
                    if not isinstance(dependency, dict):
                        continue
                    base = current
                    if dependency.get("workspace"):
                        dependency = workspace_dependencies.get(name, {})
                        base = root
                    if isinstance(dependency, dict) and isinstance(dependency.get("path"), str):
                        pending.append((base / dependency["path"]).resolve())
    return found


def _locked_dependencies(root: Path, crates: set[Path]) -> bytes:
    """Hash only reachable lock entries, so unrelated crate bumps do not cascade."""
    lock = root / "Cargo.lock"
    if not lock.exists():
        return b""
    packages = _toml(lock).get("package", [])
    selected: dict[str, dict[str, Any]] = {}
    pending = [_toml(crate / "Cargo.toml")["package"]["name"] for crate in crates]
    while pending:
        reference = pending.pop()
        parts = reference.split()
        for package in packages:
            if package["name"] != parts[0] or (len(parts) > 1 and package["version"] != parts[1]):
                continue
            key = f"{package['name']} {package['version']} {package.get('source', '')}"
            if key in selected:
                continue
            selected[key] = package
            pending.extend(package.get("dependencies", []))
    return json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()


def workspace_source_identity(root: Path, name: str) -> str:
    """Fingerprint the binary crate, recursive path dependencies and build inputs."""
    root = root.resolve()
    crates = _crate_closure(root, root / "crates" / RELEASE_CRATES[name])
    digest = hashlib.sha256()
    files: set[Path] = set()
    try:
        visible = spawn.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                *(crate.relative_to(root).as_posix() for crate in sorted(crates)),
                "Cargo.toml",
                "rust-toolchain.toml",
                ".cargo/config.toml",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BinaryReleaseVersionError(
            "workspace source identity unavailable; use a Git checkout"
        ) from exc
    for filename in visible.split(b"\0"):
        if not filename:
            continue
        path = root / os.fsdecode(filename)
        if path.is_file() and not any(
            part in {"tests", "benches", "examples", "docs"}
            for part in path.relative_to(root).parts
        ):
            files.add(path)
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    digest.update(_locked_dependencies(root, crates))
    return digest.hexdigest()


def release_identity_path(bin_dir: Path, name: str) -> Path:
    return bin_dir / f".{name}-release-identity.json"


def _same_legacy_executable(source: Path, installed: Path) -> bool:
    """Compare legacy executable bytes without modifying either signed original."""
    if sys.platform != "darwin":
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="gobby-release-identity-") as directory:
            copies = [Path(directory) / "candidate", Path(directory) / "installed"]
            for original, copy in zip((source, installed), copies, strict=True):
                shutil.copyfile(original, copy)
                # Signing can also adjust Mach-O link-edit load commands. Apply
                # the same signing policy before stripping signature blobs.
                for command in (
                    ["codesign", "-f", "-s", "-", "-i", "gobby-release-comparison", str(copy)],
                    ["codesign", "--remove-signature", str(copy)],
                ):
                    result = spawn.run(command, check=False, capture_output=True, timeout=30)
                    if result.returncode != 0:
                        return False
            return copies[0].read_bytes() == copies[1].read_bytes()
    except (OSError, subprocess.SubprocessError):
        return False


def check_binary_release(
    name: str,
    source: Path,
    *,
    bin_dir: Path,
    workspace_root: Path | None,
    source_identity: str | None = None,
) -> BinaryReleaseIdentity:
    """Fail closed before promotion when an installed version conceals new sources."""
    version = probe_native_bin_version(source)
    if version is None:
        raise BinaryReleaseVersionError(f"{name}: candidate did not report a version; rebuild it")
    artifact_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = BinaryReleaseIdentity(
        version,
        source_identity
        or (
            workspace_source_identity(workspace_root, name)
            if workspace_root is not None
            else f"artifact:{artifact_sha256}"
        ),
        artifact_sha256,
    )
    installed = bin_dir / native_bin_name(name)
    if not installed.exists():
        return candidate
    installed_version = probe_native_bin_version(installed)
    if installed_version is None:
        raise BinaryReleaseVersionError(
            f"{name}: installed version cannot be verified; "
            "remove the unverified installed binary, then rebuild and install"
        )
    if installed_version != version:
        return candidate
    record = release_identity_path(bin_dir, name)
    if record.exists():
        try:
            previous = json.loads(record.read_text())
            same_source = (
                isinstance(previous, dict)
                and previous.get("version") == version
                and previous.get("source_identity")
                in (candidate.source_identity, f"artifact:{artifact_sha256}")
            )
            if same_source and previous.get("source_identity") == f"artifact:{artifact_sha256}":
                candidate = BinaryReleaseIdentity(
                    version, f"artifact:{artifact_sha256}", artifact_sha256
                )
        except (OSError, ValueError):
            same_source = False
    else:
        # Legacy installs have no tree receipt. Only the exact previously built
        # artifact proves a harmless re-promotion (the installed copy may be signed).
        raw_stamp = bin_dir / f".{name}-source-sha256"
        prior_hash = raw_stamp.read_text().strip() if raw_stamp.is_file() else None
        same_source = candidate.artifact_sha256 == (
            prior_hash or hashlib.sha256(installed.read_bytes()).hexdigest()
        )
        if not same_source and prior_hash is None:
            same_source = _same_legacy_executable(source, installed)
        if same_source:
            # Exact legacy bytes establish artifact identity, never the current
            # checkout's tree: the artifact may predate edits in that checkout.
            candidate = BinaryReleaseIdentity(
                version, f"artifact:{artifact_sha256}", artifact_sha256
            )
    if not same_source:
        remedy = (
            "bump its patch version before promotion"
            if workspace_root is not None
            else "the publisher must bump its patch version; install a newer published release"
        )
        raise BinaryReleaseVersionError(
            f"{name}: changed or unverified source under installed version {version}; {remedy}"
        )
    return candidate


def write_release_identity(bin_dir: Path, name: str, identity: BinaryReleaseIdentity) -> None:
    """Persist the receipt only after the staged binary replaced its destination."""
    path = release_identity_path(bin_dir, name)
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-release-", dir=bin_dir)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(identity), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
