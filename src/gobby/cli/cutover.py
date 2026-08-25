"""Transactional developer cutover for schema-aware native binaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import click

from gobby.cli.daemon import restart
from gobby.cli.utils import get_gobby_home

_PACKAGES = ("gobby-code", "gobby-daemon", "gobby-hooks", "gobby-wiki")
_BINARY_NAMES = ("gcode", "gdaemon", "ghook", "gwiki")
_PIN_PATH = Path("src/gobby/storage/schema_expected_identity.json")
_INTEGER_IDENTITY_FIELDS = ("runner_protocol", "baseline_version", "latest_version")
_STRING_IDENTITY_FIELDS = ("baseline_checksum", "latest_checksum", "assets_root_hash")


class CutoverError(RuntimeError):
    """Raised when a native-binary cutover cannot complete coherently."""


@dataclass
class _Replacement:
    destination: Path
    staging_dir: Path
    staged: Path
    backup: Path
    original_moved: bool = False
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
            os.replace(replacement.destination, replacement.backup)
            replacement.original_moved = True
            os.replace(replacement.staged, replacement.destination)
            replacement.promoted = True

    def rollback(self) -> None:
        errors: list[str] = []
        for replacement in reversed(self.replacements):
            try:
                if replacement.promoted and os.path.lexists(replacement.destination):
                    replacement.destination.unlink()
                if replacement.original_moved and os.path.lexists(replacement.backup):
                    os.replace(replacement.backup, replacement.destination)
            except OSError as exc:
                errors.append(f"{replacement.destination}: {exc}")
        self.cleanup()
        if errors:
            raise CutoverError("cutover rollback failed: " + "; ".join(errors))

    def cleanup(self) -> None:
        for replacement in self.replacements:
            replacement.cleanup()


def _stage_file(source: Path, destination: Path, *, mode: int) -> _Replacement:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-cutover-", dir=destination.parent)
    )
    try:
        staged = staging_dir / "staged"
        backup = staging_dir / "original"
        shutil.copy2(source, staged)
        staged.chmod(mode)
        return _Replacement(destination, staging_dir, staged, backup)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _stage_bytes(content: bytes, destination: Path, *, mode: int) -> _Replacement:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-cutover-", dir=destination.parent)
    )
    try:
        staged = staging_dir / "staged"
        backup = staging_dir / "original"
        staged.write_bytes(content)
        staged.chmod(mode)
        return _Replacement(destination, staging_dir, staged, backup)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _workspace_root(path: Path) -> Path:
    root = path.resolve()
    if not (root / "Cargo.toml").is_file() or not (root / "crates").is_dir():
        raise CutoverError(f"not a Gobby workspace: {root}")
    if not (root / _PIN_PATH).is_file():
        raise CutoverError(f"schema identity pin is missing: {root / _PIN_PATH}")
    return root


def _require_existing_install(bin_dir: Path) -> None:
    missing = [str(bin_dir / name) for name in _BINARY_NAMES if not (bin_dir / name).is_file()]
    if missing:
        raise CutoverError(
            "cutover requires an existing complete native install; missing: " + ", ".join(missing)
        )


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

    artifacts = {name: root / "target" / "release" / name for name in _BINARY_NAMES}
    missing = [str(path) for path in artifacts.values() if not path.is_file()]
    if missing:
        raise CutoverError("release build omitted required artifacts: " + ", ".join(missing))
    return artifacts


def _read_schema_identity(gdaemon: Path, *, cwd: Path) -> dict[str, int | str]:
    result = _run(
        [str(gdaemon), "schema", "version", "--json"],
        cwd=cwd,
        label="gdaemon schema identity probe",
        timeout=30,
    )
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CutoverError("gdaemon returned invalid schema identity JSON") from exc
    if not isinstance(parsed, dict):
        raise CutoverError("gdaemon schema identity must be a JSON object")
    values = cast(dict[str, object], parsed)
    expected_fields = {*_INTEGER_IDENTITY_FIELDS, *_STRING_IDENTITY_FIELDS}
    if set(values) != expected_fields:
        raise CutoverError(
            f"gdaemon schema identity must contain exactly {sorted(expected_fields)}"
        )

    identity: dict[str, int | str] = {}
    for field in _INTEGER_IDENTITY_FIELDS:
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise CutoverError(f"gdaemon schema identity field {field} must be an integer")
        identity[field] = value
    for field in _STRING_IDENTITY_FIELDS:
        value = values[field]
        if not isinstance(value, str) or not value:
            raise CutoverError(f"gdaemon schema identity field {field} must be a string")
        identity[field] = value
    return identity


def _pin_content(identity: dict[str, int | str]) -> bytes:
    return (json.dumps(identity, indent=2, sort_keys=True) + "\n").encode()


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
    """Build, promote, restart, and smoke one coherent schema-aware binary set."""
    _require_existing_install(bin_dir)
    artifacts = _build_artifacts(root)
    built_identity = _read_schema_identity(artifacts["gdaemon"], cwd=root)
    replacements = _ReplacementSet.stage(
        [(artifacts[name], bin_dir / name, 0o755) for name in _BINARY_NAMES]
    )

    activated = False
    try:
        replacements.promote()
        activated = True
        installed_identity = _read_schema_identity(bin_dir / "gdaemon", cwd=root)
        if installed_identity != built_identity:
            raise CutoverError(
                "installed gdaemon identity differs from the validated release artifact"
            )
        replacements.append(
            _stage_bytes(_pin_content(installed_identity), root / _PIN_PATH, mode=0o644)
        )
        replacements.promote()
        restart_daemon()
        _smoke_installed_gcode(bin_dir / "gcode", root=root)
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
    bin_dir = get_gobby_home() / "bin"

    def restart_daemon() -> None:
        ctx.invoke(restart, verbose=False, docker_flag=False)

    try:
        run_cutover(root, bin_dir, restart_daemon=restart_daemon)
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Cutover complete: gcode, gdaemon, ghook, gwiki, schema pin, and daemon agree.")
