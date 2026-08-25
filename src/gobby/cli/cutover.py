"""Transactional developer cutover for schema-aware native binaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click

from gobby.cli.daemon import restart
from gobby.cli.install_setup_gdaemon import (
    _IDENTITY_STAMP,
    GdaemonInstallError,
    _codesign,
    _probe_version,
)
from gobby.install.bin_freshness_github import SourceUnavailableError, platform_target
from gobby.install.bin_freshness_models import managed_bin_specs
from gobby.storage.schema_identity_pin import (
    SchemaIdentityError,
    pin_bytes,
    probe_identity,
    stamp_bytes,
)
from gobby.utils.native_bin import native_bin_dir, native_bin_name

_PACKAGES = ("gobby-code", "gobby-daemon", "gobby-hooks", "gobby-wiki")
_BINARY_NAMES = ("gcode", "gdaemon", "ghook", "gwiki")
_PIN_PATH = Path("src/gobby/storage/schema_expected_identity.json")
_INSTALL_METHOD = "workspace-cutover"


class CutoverError(RuntimeError):
    """Raised when a native-binary cutover cannot complete coherently."""


@dataclass
class _Replacement:
    destination: Path
    staging_dir: Path
    staged: Path
    backup: Path
    backed_up: bool = False
    promoted: bool = False

    def cleanup(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)


class _ReplacementSet:
    """Stage and transactionally promote files within their destination filesystems."""

    def __init__(self, replacements: list[_Replacement]) -> None:
        self.replacements = replacements

    @classmethod
    def stage(
        cls,
        files: list[tuple[Path, Path, int]],
    ) -> _ReplacementSet:
        replacements: list[_Replacement] = []
        try:
            for source, destination, mode in files:
                replacements.append(_stage_file(source, destination, mode=mode))
        except BaseException:
            for replacement in replacements:
                replacement.cleanup()
            raise
        return cls(replacements)

    def append(self, replacement: _Replacement) -> None:
        self.replacements.append(replacement)

    def promote(self) -> None:
        for replacement in self.replacements:
            if replacement.promoted:
                continue
            if os.path.lexists(replacement.destination):
                # Hard-link the live inode as the backup so the destination never
                # disappears: a hook exec between two renames would hit ENOENT.
                os.link(replacement.destination, replacement.backup, follow_symlinks=False)
                replacement.backed_up = True
            os.replace(replacement.staged, replacement.destination)
            replacement.promoted = True

    def rollback(self) -> None:
        errors: list[str] = []
        for replacement in reversed(self.replacements):
            try:
                if replacement.backed_up:
                    os.replace(replacement.backup, replacement.destination)
                elif replacement.promoted and os.path.lexists(replacement.destination):
                    replacement.destination.unlink()
            except OSError as exc:
                errors.append(f"{replacement.destination}: {exc}")
        if errors:
            kept = ", ".join(
                str(replacement.backup)
                for replacement in self.replacements
                if os.path.lexists(replacement.backup)
            )
            raise CutoverError(
                "cutover rollback failed: " + "; ".join(errors) + f"; backups kept at: {kept}"
            )
        self.cleanup()

    def cleanup(self) -> None:
        for replacement in self.replacements:
            replacement.cleanup()


def _stage(destination: Path, write: Callable[[Path], object], *, mode: int) -> _Replacement:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-cutover-", dir=destination.parent)
    )
    try:
        staged = staging_dir / destination.name
        write(staged)
        staged.chmod(mode)
        backup = staging_dir / f"{destination.name}.backup"
        return _Replacement(destination, staging_dir, staged, backup)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _stage_file(source: Path, destination: Path, *, mode: int) -> _Replacement:
    return _stage(destination, lambda staged: shutil.copy2(source, staged), mode=mode)


def _stage_bytes(content: bytes, destination: Path, *, mode: int) -> _Replacement:
    return _stage(destination, lambda staged: staged.write_bytes(content), mode=mode)


def _workspace_root(path: Path) -> Path:
    root = path.resolve()
    if not (root / "Cargo.toml").is_file() or not (root / "crates").is_dir():
        raise CutoverError(f"not a Gobby workspace: {root}")
    if not (root / _PIN_PATH).is_file():
        raise CutoverError(f"schema identity pin is missing: {root / _PIN_PATH}")
    return root


def _require_existing_install(bin_dir: Path) -> None:
    missing: list[str] = []
    symlinked: list[str] = []
    for name in _BINARY_NAMES:
        path = bin_dir / native_bin_name(name)
        if path.is_symlink():
            symlinked.append(str(path))
        elif not path.is_file():
            missing.append(str(path))
    if missing:
        raise CutoverError(
            "cutover requires an existing complete native install; missing: " + ", ".join(missing)
        )
    if symlinked:
        raise CutoverError(
            "cutover refuses to replace symlinked dev-install binaries; re-point the link at the "
            "new build instead: " + ", ".join(symlinked)
        )


def _platform_target() -> str:
    try:
        return platform_target()
    except SourceUnavailableError as exc:
        raise CutoverError(str(exc)) from exc


def _run(
    args: list[str],
    *,
    cwd: Path,
    label: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # nosec B603 - fixed developer-tool arguments
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutoverError(f"{label} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise CutoverError(f"{label} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CutoverError(f"{label} failed: {detail}")
    return result


def _build_artifacts(root: Path) -> dict[str, Path]:
    args = ["cargo", "build", "--release", "--locked"]
    for package in _PACKAGES:
        args.extend(("-p", package))
    _run(args, cwd=root, label="release build", timeout=1800)

    artifacts = {
        name: root / "target" / "release" / native_bin_name(name) for name in _BINARY_NAMES
    }
    missing = [str(path) for path in artifacts.values() if not path.is_file()]
    if missing:
        raise CutoverError("release build omitted required artifacts: " + ", ".join(missing))
    return artifacts


def _read_schema_identity(gdaemon: Path, *, cwd: Path) -> dict[str, int | str]:
    try:
        return probe_identity(gdaemon, cwd=cwd)
    except SchemaIdentityError as exc:
        raise CutoverError(str(exc)) from exc


def _sign(binary: Path) -> None:
    try:
        _codesign(binary)
    except GdaemonInstallError as exc:
        raise CutoverError(str(exc)) from exc


def _installed_version(binary: Path) -> str:
    version = _probe_version(binary)
    if version is None:
        raise CutoverError(f"installed {binary.name} did not report a version")
    return version


def _stage_sidecars(
    replacements: _ReplacementSet,
    bin_dir: Path,
    identity: dict[str, int | str],
    *,
    target: str,
) -> None:
    """Stage the installer's version stamps, install sidecars, and identity stamp."""
    installed_at = datetime.now(UTC).isoformat()
    specs = {spec.name: spec for spec in managed_bin_specs()}
    for name in _BINARY_NAMES:
        spec = specs[name]
        version = _installed_version(bin_dir / native_bin_name(name))
        sidecar = {
            "install_method": _INSTALL_METHOD,
            "install_source_url": None,
            "installed_version": version,
            "installed_at": installed_at,
            "target": target,
        }
        replacements.append(
            _stage_bytes(f"{version}\n".encode(), bin_dir / spec.stamp_name, mode=0o644)
        )
        replacements.append(
            _stage_bytes(
                (json.dumps(sidecar, sort_keys=True) + "\n").encode(),
                bin_dir / spec.sidecar_name,
                mode=0o644,
            )
        )
    replacements.append(_stage_bytes(stamp_bytes(identity), bin_dir / _IDENTITY_STAMP, mode=0o644))


def _smoke_installed_gcode(gcode: Path, *, root: Path) -> None:
    _run(
        [
            str(gcode),
            "grep",
            "-F",
            "__gobby_cutover_schema_identity_smoke__",
            "-m",
            "1",
            "--format",
            "json",
        ],
        cwd=root,
        label="installed gcode grant smoke",
        timeout=60,
    )


def run_cutover(
    root: Path,
    bin_dir: Path,
    *,
    restart_daemon: Callable[[], None],
) -> None:
    """Build, sign, promote, stamp, restart, and smoke one coherent schema-aware binary set."""
    _require_existing_install(bin_dir)
    target = _platform_target()
    artifacts = _build_artifacts(root)
    built_identity = _read_schema_identity(artifacts["gdaemon"], cwd=root)
    replacements = _ReplacementSet.stage(
        [(artifacts[name], bin_dir / native_bin_name(name), 0o755) for name in _BINARY_NAMES]
    )

    activated = False
    try:
        for replacement in replacements.replacements:
            _sign(replacement.staged)
        replacements.promote()
        activated = True
        installed_identity = _read_schema_identity(bin_dir / native_bin_name("gdaemon"), cwd=root)
        if installed_identity != built_identity:
            raise CutoverError(
                "installed gdaemon identity differs from the validated release artifact"
            )
        _stage_sidecars(replacements, bin_dir, installed_identity, target=target)
        replacements.append(
            _stage_bytes(pin_bytes(installed_identity), root / _PIN_PATH, mode=0o644)
        )
        replacements.promote()
        restart_daemon()
        _smoke_installed_gcode(bin_dir / native_bin_name("gcode"), root=root)
    except BaseException as exc:
        try:
            replacements.rollback()
            if activated:
                restart_daemon()
        except BaseException as rollback_exc:
            raise CutoverError(
                f"cutover failed ({exc}); rollback or prior-daemon restart also failed: "
                f"{rollback_exc}"
            ) from rollback_exc
        if activated:
            raise CutoverError(f"cutover failed; restored prior install: {exc}") from exc
        raise CutoverError(f"cutover promotion failed; restored prior install: {exc}") from exc
    replacements.cleanup()


@click.command()
@click.option(
    "--path",
    "workspace",
    type=click.Path(path_type=Path, file_okay=False),
    default=".",
    show_default=True,
    help="Gobby workspace containing Cargo.toml and crates/.",
)
@click.pass_context
def cutover(ctx: click.Context, workspace: Path) -> None:
    """Build and atomically activate all schema-aware native binaries."""
    root = _workspace_root(workspace)
    bin_dir = native_bin_dir()

    def restart_daemon() -> None:
        try:
            ctx.invoke(restart, verbose=False, docker_flag=False)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise CutoverError(f"daemon restart failed (exit {exc.code})") from exc

    try:
        run_cutover(root, bin_dir, restart_daemon=restart_daemon)
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Cutover complete: gcode, gdaemon, ghook, gwiki, schema pin, and daemon agree.")
