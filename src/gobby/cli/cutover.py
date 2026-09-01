"""Build and activate one coherent set of schema-aware native binaries."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import click

from gobby.cli.daemon import restart
from gobby.install.bin_freshness_github import SourceUnavailableError, platform_target
from gobby.install.bin_set_coherence import (
    IDENTITY_STAMP_NAME,
    REBUILD_REMEDY,
    BinarySetCoherenceError,
    WorkspacePromotionMetadata,
    probe_set_member_identity,
    promote_workspace_binary_set,
)
from gobby.storage.schema_identity_pin import SchemaIdentityError, validate_identity
from gobby.utils.native_bin import native_bin_dir, native_bin_name, resolve_native_bin

_PACKAGES = ("gobby-code", "gobby-daemon", "gobby-hooks", "gobby-wiki")
_BINARY_NAMES = ("gcode", "gdaemon", "ghook", "gwiki")
_PIN_PATH = Path("src/gobby/storage/schema_expected_identity.json")
_INSTALL_METHOD = "workspace-cutover"


class CutoverError(RuntimeError):
    """Raised when a native-binary cutover cannot complete coherently."""


def _workspace_root(path: Path) -> Path:
    root = path.resolve()
    if not (root / "Cargo.toml").is_file() or not (root / "crates").is_dir():
        raise CutoverError(f"not a Gobby workspace: {root}")
    if not (root / _PIN_PATH).is_file():
        raise CutoverError(f"schema identity pin is missing: {root / _PIN_PATH}")
    return root


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


def _read_installed_pin(bin_dir: Path) -> dict[str, int | str]:
    pin_path = bin_dir / IDENTITY_STAMP_NAME
    try:
        parsed: object = json.loads(pin_path.read_text(encoding="utf-8"))
        return validate_identity(parsed)
    except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
        raise CutoverError(f"installed schema identity pin is unreadable: {exc}") from exc


def _render_identity(identity: dict[str, int | str]) -> str:
    contract = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"v{identity['latest_version']} {contract}"


def _verify_restart_target(bin_dir: Path) -> None:
    pin = _read_installed_pin(bin_dir)
    resolved = resolve_native_bin("gdaemon")
    if resolved is None:
        raise CutoverError(f"the daemon restart gdaemon is unavailable; {REBUILD_REMEDY}")
    resolved_path = Path(resolved)
    try:
        identity = probe_set_member_identity(resolved_path, "gdaemon")
    except BinarySetCoherenceError as exc:
        raise CutoverError(
            f"the daemon restart gdaemon {resolved_path} is unreadable: {exc}; {REBUILD_REMEDY}"
        ) from exc
    if identity != pin:
        raise CutoverError(
            f"the daemon restart gdaemon {resolved_path} identity {_render_identity(identity)} "
            f"differs from installed pin {_render_identity(pin)}; {REBUILD_REMEDY}"
        )


def run_cutover(
    root: Path,
    bin_dir: Path,
    *,
    restart_daemon: Callable[[], None],
) -> None:
    """Build, promote, verify, and restart one coherent native-binary set."""
    artifacts = _build_artifacts(root)
    try:
        promote_workspace_binary_set(
            artifacts,
            bin_dir=bin_dir,
            metadata=WorkspacePromotionMetadata(
                install_method=_INSTALL_METHOD,
                target=_platform_target(),
            ),
        )
    except BinarySetCoherenceError as exc:
        raise CutoverError(str(exc)) from exc
    _verify_restart_target(bin_dir)
    restart_daemon()


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
    """Build and activate all schema-aware native binaries as one set."""
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
