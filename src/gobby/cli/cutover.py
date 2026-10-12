"""Build and activate one coherent set of schema-aware native binaries."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import click

from gobby.agents.cargo_target import cargo_release_dir
from gobby.cli._daemon_handoffs import protect_pending_handoffs
from gobby.cli._daemon_protected_runs import clear_protected_runs, fetch_protected_runs
from gobby.cli.daemon_lifecycle import _do_stop
from gobby.cli.daemon_preflight import restart_start_refusal
from gobby.cli.daemon_start import start
from gobby.cli.runtime import get_cli_runtime
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap, node_daemon_refusal
from gobby.install.bin_freshness_github import SourceUnavailableError, platform_target
from gobby.install.bin_set_coherence import (
    REBUILD_REMEDY,
    BinarySetCoherenceError,
    WorkspacePromotionMetadata,
    probe_set_member_identity,
    promote_workspace_binary_set,
)
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked
from gobby.storage.schema_identity_pin import SchemaIdentityError, validate_identity
from gobby.utils.native_bin import (
    IDENTITY_STAMP_NAME,
    native_bin_dir,
    native_bin_name,
    resolve_native_bin,
)

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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # nosec B603 # fixed developer-tool arguments
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutoverError(f"{label} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise CutoverError(f"{label} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
        detail = detail or f"exit {result.returncode}"
        raise CutoverError(f"{label} failed: {detail}")
    return result


def _build_artifacts(root: Path) -> dict[str, Path]:
    args = ["cargo", "build", "--release", "--locked"]
    for package in _PACKAGES:
        args.extend(("-p", package))
    _run(args, cwd=root, label="release build", timeout=1800)

    artifacts = {
        name: cargo_release_dir(root, cwd=root) / native_bin_name(name) for name in _BINARY_NAMES
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


def _report(message: str, *, error: bool = False) -> None:
    click.echo(message, err=error)


def _stop_admission_refusal(ctx: click.Context, *, wait: bool) -> str | None:
    """Apply the restart's stop-time admissions before anything is promoted.

    A protected cron run or an unresolved session handoff refuses the stop, so
    checking them only at restart time would refuse after promotion and leave
    the old daemon running against the new binary set.
    """
    runtime = get_cli_runtime(ctx)
    port = runtime.read_only_operational_config().daemon_port
    if not clear_protected_runs(
        port, force=False, wait=wait, step=_report, fetch=fetch_protected_runs
    ):
        return "a restart-protected cron run is active; re-run with --wait"
    try:
        with protect_pending_handoffs(runtime, wait=wait, report=_report):
            pass
    except HandoffShutdownBlocked as exc:
        return f"{exc}; re-run with --wait"
    return None


def _prove_catalog_freshness(root: Path, candidate: Path) -> None:
    """Bind the migrated planned catalog to the exact candidate schema assets."""
    try:
        identity = probe_set_member_identity(candidate, "gdaemon")
        env = {**os.environ, "GOBBY_EXPECTED_SCHEMA_IDENTITY": json.dumps(identity)}
        env.pop("UPDATE_GCORE_SCHEMA_MANIFEST", None)
        result = _run(
            [
                "cargo",
                "test",
                "--locked",
                "-p",
                "gobby-core",
                "--features",
                "postgres",
                "--test",
                "catalog_manifest_freshness",
                "catalog_manifest_is_fresh_for_embedded_assets",
                "--",
                "--exact",
            ],
            cwd=root,
            label="catalog manifest freshness",
            timeout=600,
            env=env,
        )
        if "test result: ok. 1 passed; 0 failed; 0 ignored;" not in result.stdout:
            raise CutoverError("mandatory catalog manifest freshness test did not run")
    except (BinarySetCoherenceError, CutoverError) as exc:
        raise CutoverError(
            f"refusing to promote: {exc}. Start the isolated test hub or set "
            "GOBBY_SCHEMA_TEST_DATABASE_URL; regenerate stale catalog/identity carriers"
        ) from exc


def run_cutover(
    root: Path,
    bin_dir: Path,
    *,
    stop_daemon: Callable[[], bool],
    start_daemon: Callable[[], None],
    start_refusal: Callable[[Path], str | None],
) -> None:
    """Build and prove the candidate, stop, then promote and start the new set."""
    artifacts = _build_artifacts(root)
    if refusal := start_refusal(artifacts["gdaemon"]):
        raise CutoverError(f"refusing to promote: {refusal}")
    _prove_catalog_freshness(root, artifacts["gdaemon"])
    # Only the actual stop proves that the running route accepts shutdown and
    # that the old process exited. Never replace its binaries before that proof.
    if not stop_daemon():
        raise CutoverError("refusing to promote: daemon stop failed; installed binaries unchanged")
    try:
        promote_workspace_binary_set(
            artifacts,
            bin_dir=bin_dir,
            metadata=WorkspacePromotionMetadata(
                install_method=_INSTALL_METHOD,
                target=_platform_target(),
            ),
        )
        _verify_restart_target(bin_dir)
    except (BinarySetCoherenceError, CutoverError, OSError) as exc:
        raise CutoverError(
            f"{exc}. The daemon is stopped. After resolving this failure, run `gobby start`."
        ) from exc
    start_daemon()


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
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for protected cron runs and unresolved session handoffs before promoting.",
)
@click.pass_context
def cutover(ctx: click.Context, workspace: Path, allow_dirty: bool, wait: bool) -> None:
    """Build and activate all schema-aware native binaries as one set."""
    # A node never runs the Python daemon, so refuse before any build or promotion.
    with contextlib.suppress(BootstrapConfigError):
        if refusal := node_daemon_refusal(load_bootstrap()):
            raise click.ClickException(refusal)
    root = _workspace_root(workspace)
    bin_dir = native_bin_dir()

    def stop_daemon() -> bool:
        return _do_stop(ctx, False, shutdown_intent="restart", wait=True)

    def start_daemon() -> None:
        try:
            ctx.invoke(start, verbose=False)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise CutoverError(f"daemon start failed (exit {exc.code})") from exc

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
            stop_daemon=stop_daemon,
            start_daemon=start_daemon,
            start_refusal=lambda candidate: (
                restart_start_refusal(ctx, candidate, expected_identity=expected_identity)
                or _stop_admission_refusal(ctx, wait=wait)
            ),
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Cutover complete: gcode, gdaemon, ghook, schema pin, and daemon agree.")
