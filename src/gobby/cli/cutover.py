"""Build and activate one coherent set of schema-aware native binaries."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import click

from gobby.agents.cargo_target import cargo_release_dir
from gobby.cli._daemon_handoffs import protect_pending_handoffs
from gobby.cli._daemon_protected_runs import clear_protected_runs, fetch_protected_runs
from gobby.cli.daemon_lifecycle import START_AFTER_OWN_STOP, _do_stop
from gobby.cli.daemon_preflight import restart_start_refusal
from gobby.cli.daemon_start import start
from gobby.cli.runtime import get_cli_runtime
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap, node_daemon_refusal
from gobby.install.bin_freshness_github import SourceUnavailableError, platform_target
from gobby.install.bin_freshness_promotion import preflight_binary_promotions
from gobby.install.bin_release_identity import BinaryReleaseVersionError, workspace_source_identity
from gobby.install.bin_set_coherence import (
    REBUILD_REMEDY,
    BinarySetCoherenceError,
    WorkspacePromotionMetadata,
    probe_set_member_identity,
    promote_workspace_binary_set,
)
from gobby.install.version_probe import probe_native_bin_version
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked
from gobby.storage.schema_identity_pin import SchemaIdentityError, validate_identity
from gobby.utils.native_bin import (
    IDENTITY_STAMP_NAME,
    native_bin_dir,
    native_bin_name,
    resolve_native_bin,
)

CATALOG_CANDIDATE_BUILD_TIMEOUT_SECONDS = 1200
CATALOG_FRESHNESS_PROOF_TIMEOUT_SECONDS = 600

_PACKAGES = ("gobby-code", "gobby-daemon", "gobby-hooks")
_BINARY_NAMES = ("gcode", "gdaemon", "ghook")
# gclient shares gobby-core with the set but is promoted separately, outside the stamp.
_CLIENT_PACKAGE = "gobby-client"
_CLIENT_NAME = "gclient"
# gmcp and gterm ship from this workspace too. Each is built on its own, with the
# arguments `gobby install` uses, so a gterm (Zig) failure cannot cost the others.
_COMPANION_SELECTIONS: dict[str, tuple[str, ...]] = {
    "gmcp": ("-p", "gobby-mcp"),
    "gterm": ("-p", "gobby-terminal", "--features", "vt-engine"),
}
_BUILD_TIMEOUT_SECONDS = 1800
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


@dataclass(frozen=True)
class _Client:
    artifact: Path
    source: str  # source identity taken before the build


@dataclass(frozen=True)
class _Build:
    artifacts: dict[str, Path]
    client: _Client | str  # the built gclient, or why there is none
    companions: dict[str, _Client] = field(default_factory=dict)  # gmcp and gterm


@dataclass(frozen=True)
class CutoverOutcome:
    """What happened after the set went live: binaries replaced, and why any were not."""

    promoted: tuple[str, ...] = ()
    failures: dict[str, str] = field(default_factory=dict)


def _build_artifacts(root: Path) -> _Build:
    """Build the set and gclient in one cargo run, so gobby-core compiles once.

    gclient stays outside the coherent set: when the shared run fails, the set is
    rebuilt alone (reusing the crates the shared run finished) and only gclient is lost.
    gmcp and gterm build afterwards, and either failing refuses the cutover before stop.
    """
    args = ["cargo", "build", "--release", "--locked"]
    for package in _PACKAGES:
        args.extend(("-p", package))
    client_failure = None
    try:
        client_source = workspace_source_identity(root, _CLIENT_NAME)
        _run(
            [*args, "-p", _CLIENT_PACKAGE],
            cwd=root,
            label="release build",
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
    except (CutoverError, BinaryReleaseVersionError, OSError, ValueError) as exc:
        client_failure = str(exc)
        _run(args, cwd=root, label="release build", timeout=_BUILD_TIMEOUT_SECONDS)

    release_dir = cargo_release_dir(root, cwd=root)
    artifacts = {name: release_dir / native_bin_name(name) for name in _BINARY_NAMES}
    missing = [str(path) for path in artifacts.values() if not path.is_file()]
    if missing:
        raise CutoverError("release build omitted required artifacts: " + ", ".join(missing))
    companions = _build_companions(root, release_dir)
    if client_failure is not None:
        return _Build(artifacts, client_failure, companions)
    client = release_dir / native_bin_name(_CLIENT_NAME)
    if not client.is_file():
        return _Build(artifacts, f"release build omitted {client}", companions)
    return _Build(artifacts, _Client(client, client_source), companions)


def _build_companions(root: Path, release_dir: Path) -> dict[str, _Client]:
    """Build gmcp and gterm one at a time; a failure refuses before anything stops."""
    built: dict[str, _Client] = {}
    for name, selection in _COMPANION_SELECTIONS.items():
        try:
            source = workspace_source_identity(root, name)
        except (BinaryReleaseVersionError, OSError, ValueError) as exc:
            raise CutoverError(f"refusing to promote: {name} source identity: {exc}") from exc
        _run(
            ["cargo", "build", "--release", "--locked", *selection],
            cwd=root,
            label=f"{name} release build",
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
        artifact = release_dir / native_bin_name(name)
        if not artifact.is_file():
            raise CutoverError(f"{name} release build omitted {artifact}")
        built[name] = _Client(artifact, source)
    return built


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


def read_installed_pin(bin_dir: Path) -> dict[str, int | str]:
    pin_path = bin_dir / IDENTITY_STAMP_NAME
    try:
        parsed: object = json.loads(pin_path.read_text(encoding="utf-8"))
        return validate_identity(parsed)
    except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
        raise CutoverError(f"installed schema identity pin is unreadable: {exc}") from exc


def read_workspace_pin(root: Path) -> dict[str, int | str]:
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
    pin = read_installed_pin(bin_dir)
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


def _stop_admission_refusal(
    ctx: click.Context,
    *,
    wait: bool,
    logged_stale_markers: set[tuple[str, str | None, str]] | None = None,
) -> str | None:
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
        with protect_pending_handoffs(
            runtime, wait=wait, report=_report, logged_stale_markers=logged_stale_markers
        ):
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
            timeout=CATALOG_FRESHNESS_PROOF_TIMEOUT_SECONDS,
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
) -> CutoverOutcome:
    """Build and prove the candidate, stop, then promote and start the new set.

    gmcp, gterm and gclient are promoted after the daemon is back. gmcp and gterm
    pass the same release preflight as the set before anything stops, so a changed
    source under an unbumped version refuses the cutover instead of leaving them stale.
    """
    try:
        sources = {name: workspace_source_identity(root, name) for name in _BINARY_NAMES}
        build = _build_artifacts(root)
        artifacts = build.artifacts
        companion_sources = {name: built.source for name, built in build.companions.items()}
        preflight_binary_promotions(
            {**artifacts, **{name: built.artifact for name, built in build.companions.items()}},
            bin_dir=bin_dir,
            workspace_root=root,
            source_identities={**sources, **companion_sources},
        )
    except BinaryReleaseVersionError as exc:
        raise CutoverError(f"refusing to promote: {exc}") from exc
    if refusal := start_refusal(artifacts["gdaemon"]):
        raise CutoverError(f"refusing to promote: {refusal}")
    _prove_catalog_freshness(root, artifacts["gdaemon"])
    _require_unchanged_sources(root, {**sources, **companion_sources})
    # Only the actual stop proves that the running route accepts shutdown and
    # that the old process exited. Never replace its binaries before that proof.
    if not stop_daemon():
        raise CutoverError("refusing to promote: daemon stop failed; installed binaries unchanged")
    from gobby.releases.activation import agent_activation_refusal

    activation_refusal: str | None = None
    try:
        _require_unchanged_sources(root, sources)
        # The activation check runs again after the stop. Nothing is promoted yet, so a
        # refusal leaves the unchanged daemon to start again and the cutover is refused.
        activation_refusal = agent_activation_refusal("cutover")
        if activation_refusal is None:
            promote_workspace_binary_set(
                artifacts,
                bin_dir=bin_dir,
                workspace_root=root,
                source_identities=sources,
                metadata=WorkspacePromotionMetadata(
                    install_method=_INSTALL_METHOD,
                    target=_platform_target(),
                ),
            )
            _verify_restart_target(bin_dir)
        start_daemon()
    except Exception as exc:
        # After confirmed shutdown, every failed phase needs operator recovery
        # guidance, including startup errors and identity decoding failures.
        refused = f"refusing to promote: {activation_refusal}; " if activation_refusal else ""
        raise CutoverError(
            f"{refused}{exc}. The daemon is stopped. After resolving this failure, run "
            "`gobby start`."
        ) from exc
    if activation_refusal is not None:
        raise CutoverError(
            f"refusing to promote: {activation_refusal}; restarted the unchanged daemon"
        )
    promoted: list[str] = []
    failures: dict[str, str] = {}
    for name, companion in build.companions.items():
        try:
            if _promote_companion(root, bin_dir, name, companion) == "promoted":
                promoted.append(name)
        except _POST_START_ERRORS as exc:
            failures[name] = str(exc)
    if isinstance(build.client, str):
        failures[_CLIENT_NAME] = build.client
    else:
        try:
            _promote_client(root, bin_dir, build.client)
        except _POST_START_ERRORS as exc:
            failures[_CLIENT_NAME] = str(exc)
    return CutoverOutcome(tuple(promoted), failures)


_POST_START_ERRORS = (
    CutoverError,
    BinarySetCoherenceError,
    BinaryReleaseVersionError,
    OSError,
    ValueError,
)


def _promote_companion(root: Path, bin_dir: Path, name: str, companion: _Client) -> str:
    """Promote gmcp or gterm when stale; returns "promoted" or "current"."""
    from gobby.cli import install_setup

    promote, write_stamp = {
        "gmcp": (
            install_setup._promote_gmcp_workspace_artifact,
            install_setup._write_gmcp_version_stamp,
        ),
        "gterm": (
            install_setup._promote_gterm_workspace_artifact,
            install_setup._write_gterm_version_stamp,
        ),
    }[name]
    _require_unchanged_sources(root, {name: companion.source})
    outcome = promote(companion.artifact, bin_dir, root)
    if outcome is None:
        raise CutoverError(f"{name}: native binary update is already in progress")
    if version := probe_native_bin_version(bin_dir / native_bin_name(name)):
        write_stamp(bin_dir, version)
    return outcome


def _promote_client(root: Path, bin_dir: Path, client: _Client) -> None:
    from gobby.cli import install_setup

    _require_unchanged_sources(root, {_CLIENT_NAME: client.source})
    if install_setup._promote_gclient_workspace_artifact(client.artifact, bin_dir, root) is None:
        raise CutoverError("gclient: native binary update is already in progress")
    if version := install_setup._get_installed_gclient_version(bin_dir):
        install_setup._write_gclient_version_stamp(bin_dir, version)


def _require_unchanged_sources(root: Path, sources: dict[str, str]) -> None:
    for name, identity in sources.items():
        if workspace_source_identity(root, name) != identity:
            raise CutoverError(f"{name}: source changed during cutover; rebuild before promotion")


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
    from gobby.releases.activation import agent_activation_refusal

    if refusal := agent_activation_refusal("cutover"):
        raise click.ClickException(f"Refusing to cut over: {refusal}")
    # A node never runs the Python daemon, so refuse before any build or promotion.
    with contextlib.suppress(BootstrapConfigError):
        if refusal := node_daemon_refusal(load_bootstrap()):
            raise click.ClickException(refusal)
    root = _workspace_root(workspace)
    bin_dir = native_bin_dir()
    logged_stale_markers: set[tuple[str, str | None, str]] = set()

    def stop_daemon() -> bool:
        return _do_stop(
            ctx,
            False,
            shutdown_intent="restart",
            wait=True,
            logged_stale_markers=logged_stale_markers,
        )

    def start_daemon() -> None:
        ctx.meta[START_AFTER_OWN_STOP] = True
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
        expected_identity = read_workspace_pin(root)
        runtime = get_cli_runtime(ctx)
        outcome = run_cutover(
            root,
            bin_dir,
            stop_daemon=stop_daemon,
            start_daemon=start_daemon,
            start_refusal=lambda candidate: (
                restart_start_refusal(runtime, candidate, expected_identity=expected_identity)
                or _stop_admission_refusal(
                    ctx, wait=wait, logged_stale_markers=logged_stale_markers
                )
            ),
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from exc
    agreement = "gcode, gdaemon, ghook, schema pin, and daemon agree."
    if outcome.failures:
        click.echo(f"Cutover set is live: {agreement}")
    else:
        click.echo(f"Cutover complete: {agreement}")
    if outcome.promoted:
        click.echo("Promoted: " + ", ".join(outcome.promoted) + ".")
    if _CLIENT_NAME not in outcome.failures:
        click.echo("gclient matches this build.")
    for name, failure in outcome.failures.items():
        _report(
            f"{name} was not promoted: {failure}. The daemon set is live; "
            f"after resolving this, run `uv run gobby install {name}`.",
            error=True,
        )
