"""Build and activate one coherent set of schema-aware native binaries."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import click

from gobby.cli.daemon import restart
from gobby.cli.daemon_preflight import restart_start_refusal
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

_PACKAGES = ("gobby-code", "gobby-daemon", "gobby-hooks")
_BINARY_NAMES = ("gcode", "gdaemon", "ghook")
_PIN_PATH = Path("src/gobby/storage/schema_expected_identity.json")
_INSTALL_METHOD = "workspace-cutover"
_SCHEMA_INPUTS = (
    "crates/gcore/assets/schema",
    "crates/gcore/src/schema",
    str(_PIN_PATH),
)


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


def _dirty_schema_inputs(root: Path) -> list[str]:
    """List uncommitted schema inputs; a git failure fails the cutover closed.

    Scoped to the inputs that decide the embedded schema: a cutover that builds
    another session's uncommitted migration, baseline or identity pin ships a
    daemon whose schema apply cannot succeed. Routine non-schema dirt on the
    shared checkout is none of this gate's business.
    """
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *_SCHEMA_INPUTS],
        cwd=root,
        label="schema input status",
        timeout=60,
    )
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def _read_installed_pin(bin_dir: Path) -> dict[str, int | str]:
    pin_path = bin_dir / IDENTITY_STAMP_NAME
    try:
        parsed: object = json.loads(pin_path.read_text(encoding="utf-8"))
        return validate_identity(parsed)
    except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
        raise CutoverError(f"installed schema identity pin is unreadable: {exc}") from exc


def _read_workspace_pin(root: Path) -> dict[str, int | str]:
    pin_path = root / _PIN_PATH
    try:
        parsed: object = json.loads(pin_path.read_text(encoding="utf-8"))
        return validate_identity(parsed)
    except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
        raise CutoverError(f"workspace schema identity pin is unreadable: {exc}") from exc


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
    start_refusal: Callable[[Path], str | None],
) -> None:
    """Build, prove, promote, verify, and restart one coherent native-binary set."""
    artifacts = _build_artifacts(root)
    if refusal := start_refusal(artifacts["gdaemon"]):
        raise CutoverError(f"refusing to promote: {refusal}")
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
@click.option(
    "--allow-dirty",
    "allow_dirty",
    is_flag=True,
    help="Build even when the schema inputs have uncommitted changes.",
)
@click.pass_context
def cutover(ctx: click.Context, workspace: Path, allow_dirty: bool) -> None:
    """Build and activate all schema-aware native binaries as one set."""
    root = _workspace_root(workspace)
    bin_dir = native_bin_dir()

    def restart_daemon() -> None:
        try:
            ctx.invoke(
                restart,
                verbose=False,
                docker_flag=False,
                expected_identity=expected_identity,
            )
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise CutoverError(f"daemon restart failed (exit {exc.code})") from exc

    try:
        if not allow_dirty and (dirty := _dirty_schema_inputs(root)):
            raise CutoverError(
                "refusing to build from uncommitted schema inputs: "
                + ", ".join(dirty)
                + "; commit or stash them, or pass --allow-dirty"
            )
        expected_identity = _read_workspace_pin(root)
        run_cutover(
            root,
            bin_dir,
            restart_daemon=restart_daemon,
            start_refusal=lambda candidate: restart_start_refusal(
                ctx,
                candidate,
                expected_identity=expected_identity,
            ),
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Cutover complete: gcode, gdaemon, ghook, schema pin, and daemon agree.")
