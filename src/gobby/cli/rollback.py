"""List, pin, hold and restore retained binary generations."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click

from gobby.cli.cutover import _stop_admission_refusal, _verify_restart_target
from gobby.cli.daemon_lifecycle import START_AFTER_OWN_STOP, _do_stop
from gobby.cli.daemon_preflight import restart_start_refusal
from gobby.cli.daemon_start import start
from gobby.cli.runtime import get_cli_runtime
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap, node_daemon_refusal
from gobby.hooks.checkout_reads import run_git
from gobby.install.bin_freshness_promotion import file_sha256
from gobby.install.bin_generations import (
    MANAGED_BINARIES,
    GenerationStoreError,
    burn_version,
    clear_hold,
    extend_hold,
    extend_pin,
    find_generation,
    hold_status,
    list_generations,
    list_pins,
    pin_generation,
    set_hold,
    unpin_generation,
)
from gobby.install.bin_restore import (
    RestoreError,
    RestoreOutcome,
    restore_members,
    run_restore,
)
from gobby.tasks.landing_policy import read_freeze, write_freeze
from gobby.utils.native_bin import native_bin_dir, native_bin_name

_SESSION_ENV = "GOBBY_SESSION_ID"
_OPERATOR_SESSION = "operator"
_RELATIVE_UNTIL = re.compile(r"\+(\d+)([mh])")
_CLEARED_FREEZE_REASON = "cleared by gobby rollback hold clear"
_GIT_TIMEOUT_SECONDS = 30


class _UntilTime(click.ParamType[datetime]):
    """An ISO-8601 time (a naive one is local time) or +<N>m / +<N>h from now."""

    name = "time"

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> datetime:
        if isinstance(value, datetime):
            return value
        text = str(value)
        if match := _RELATIVE_UNTIL.fullmatch(text):
            unit_minutes = 60 if match.group(2) == "h" else 1
            return datetime.now(UTC) + timedelta(minutes=int(match.group(1)) * unit_minutes)
        try:
            return datetime.fromisoformat(text).astimezone(UTC)
        except ValueError:
            self.fail(f"{text!r} is not an ISO-8601 time or +<N>m / +<N>h", param, ctx)


_UNTIL = _UntilTime()
_UNTIL_HELP = "ISO-8601 time (naive is local), or +<N>m / +<N>h from now."


@contextlib.contextmanager
def _reported() -> Iterator[None]:
    """Report a store or restore refusal as a non-zero exit that carries its message."""
    try:
        yield
    except (GenerationStoreError, RestoreError) as exc:
        raise click.ClickException(str(exc)) from exc


def _session() -> str:
    return os.environ.get(_SESSION_ENV) or _OPERATOR_SESSION


def _installed_sha256(bin_dir: Path, name: str) -> str | None:
    path = bin_dir / native_bin_name(name)
    return file_sha256(path) if path.is_file() else None


def _checkout_common_dir(path: Path) -> Path | None:
    """The git common directory of the checkout at path, or None when path is not in one."""
    try:
        result = run_git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(path),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    common = result.stdout.decode("utf-8", errors="replace").strip()
    return Path(common) if result.returncode == 0 and common else None


@click.group("rollback")
def rollback() -> None:
    """List, pin and hold retained binary generations, and restore one as the installed set."""


def _generation_rows(bin_dir: Path, now: datetime) -> list[dict[str, Any]]:
    pins = list_pins(bin_dir, now=now)
    hold = hold_status(bin_dir, now=now)
    held_from = hold["first_seq"] if hold is not None and not hold["expired"] else None
    installed = _installed_sha256(bin_dir, "gdaemon")
    rows: list[dict[str, Any]] = []
    for generation in list_generations(bin_dir):
        recorded: dict[str, Any] = generation.manifest["members"]
        pin = pins.get(generation.id)
        rows.append(
            {
                "id": generation.id,
                "seq": generation.seq,
                "created_at": generation.manifest["created_at"],
                "members": {name: record.get("version") for name, record in recorded.items()},
                "pinned": pin is not None and not pin["expired"],
                "held": held_from is not None and generation.seq >= held_from,
                "installed": installed is not None
                and recorded.get("gdaemon", {}).get("sha256") == installed,
            }
        )
    return rows


def _describe(row: dict[str, Any]) -> str:
    members = ", ".join(
        f"{name} {version or 'unversioned'}" for name, version in row["members"].items()
    )
    markers = [label for label in ("pinned", "held", "installed") if row[label]]
    suffix = f"  [{', '.join(markers)}]" if markers else ""
    return f"#{row['seq']} {row['id']}  {members}{suffix}"


@rollback.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print a JSON array, oldest first.")
def _list_command(as_json: bool) -> None:
    """List the retained generations, oldest first."""
    with _reported():
        rows = _generation_rows(native_bin_dir(), datetime.now(UTC))
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No generations are retained.")
    for row in rows:
        click.echo(_describe(row))


@click.command("pin")
@click.argument("generation")
@click.option("--until", "until", type=_UNTIL, required=True, help=_UNTIL_HELP)
@click.option("--task", "task_id", default=None, help="Task that holds the pin, by reference.")
def _pin_generation(generation: str, until: datetime, task_id: str | None) -> None:
    """Pin GENERATION (id or sequence number) until a time, so pruning keeps it."""
    bin_dir = native_bin_dir()
    with _reported():
        target = find_generation(bin_dir, generation)
        pin_generation(bin_dir, target.id, until=until, task_id=task_id, session_id=_session())
        click.echo(f"Pinned generation {target.id} until {until.isoformat()}.")


class _PinGroup(click.Group):
    """`pin GENERATION --until T` pins; `pin extend GENERATION --until T` moves a pin."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args[0] in self.commands:
            return super().resolve_command(ctx, args)
        return "pin", _pin_generation, args


@rollback.group("pin", cls=_PinGroup, context_settings={"ignore_unknown_options": True})
def _pin() -> None:
    """Pin a generation until a time; `pin extend` moves the expiry of an existing pin."""


@_pin.command("extend")
@click.argument("generation")
@click.option("--until", "until", type=_UNTIL, required=True, help=_UNTIL_HELP)
def _pin_extend(generation: str, until: datetime) -> None:
    """Move the expiry of GENERATION's pin to a time."""
    bin_dir = native_bin_dir()
    with _reported():
        target = find_generation(bin_dir, generation)
        extend_pin(bin_dir, target.id, until=until)
        click.echo(f"Extended the pin on generation {target.id} until {until.isoformat()}.")


@rollback.command("unpin")
@click.argument("generation")
def _unpin(generation: str) -> None:
    """Remove GENERATION's pin, if it has one."""
    bin_dir = native_bin_dir()
    with _reported():
        target = find_generation(bin_dir, generation)
        if unpin_generation(bin_dir, target.id):
            click.echo(f"Unpinned generation {target.id}.")
        else:
            click.echo(f"Generation {target.id} was not pinned.")


@rollback.group("hold")
def _hold() -> None:
    """Keep the retained generations of a window from pruning, so a rollback can reach them."""


@_hold.command("set")
@click.option("--task", "task_id", required=True, help="Task that owns the hold, by reference.")
@click.option(
    "--session",
    "session_id",
    default=None,
    help=f"Session that owns the hold (default: ${_SESSION_ENV}, else {_OPERATOR_SESSION}).",
)
@click.option("--until", "until", type=_UNTIL, required=True, help=_UNTIL_HELP)
def _hold_set(task_id: str, session_id: str | None, until: datetime) -> None:
    """Hold the generations until a time; prints the token that extends or ends it."""
    with _reported():
        token = set_hold(
            native_bin_dir(),
            task_id=task_id,
            session_id=session_id or _session(),
            expires_at=until,
        )
    click.echo(f"Hold set for task {task_id} until {until.isoformat()}.")
    click.echo(f"hold token: {token}")


@_hold.command("extend")
@click.option("--hold-token", "token", required=True, help="Token printed by hold set.")
@click.option("--until", "until", type=_UNTIL, required=True, help=_UNTIL_HELP)
def _hold_extend(token: str, until: datetime) -> None:
    """Move the expiry of the stored hold for the holder of its token."""
    with _reported():
        extend_hold(native_bin_dir(), token, expires_at=until)
    click.echo(f"Hold extended until {until.isoformat()}.")


@_hold.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print the hold as a JSON object.")
def _hold_status(as_json: bool) -> None:
    """Show the stored hold. The token is never printed."""
    with _reported():
        hold = hold_status(native_bin_dir(), now=datetime.now(UTC))
    if as_json:
        click.echo(json.dumps(hold))
    elif hold is None:
        click.echo("No generation hold is set.")
    else:
        state = "expired" if hold["expired"] else "active"
        click.echo(
            f"Hold ({state}) for task {hold['task_id']} by session {hold['session_id']}, "
            f"set {hold['set_at']}, expires {hold['expires_at']}."
        )


@_hold.command("clear")
@click.option(
    "--path",
    "checkout",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Checkout whose task-bound landing freeze is also cleared.",
)
def _hold_clear(checkout: Path) -> None:
    """Clear the stored hold and any task-bound landing freeze of the checkout."""
    from gobby.releases.activation import agent_activation_refusal

    if refusal := agent_activation_refusal("hold clear"):
        raise click.ClickException(refusal)
    with _reported():
        cleared = clear_hold(native_bin_dir())
    click.echo("Cleared the generation hold." if cleared else "No generation hold was stored.")
    common = _checkout_common_dir(checkout)
    if common is None:
        click.echo(
            "Left the landing freeze alone: no checkout freeze was checked, because the path "
            "is not inside a git checkout."
        )
        return
    freeze = read_freeze(common)
    if not (freeze.on and freeze.task_id is not None):
        click.echo("Left the landing freeze alone: it is off or not bound to a task.")
        return
    try:
        write_freeze(
            common, on=False, reason=_CLEARED_FREEZE_REASON, session_id=_session(), task_id=None
        )
    except OSError as exc:
        raise click.ClickException(f"Cannot write the landing freeze: {exc}") from exc
    click.echo(f"Cleared the landing freeze bound to task {freeze.task_id}.")


@rollback.command("burn")
@click.argument("generation")
@click.option(
    "--bin",
    "name",
    type=click.Choice(MANAGED_BINARIES),
    default="gdaemon",
    show_default=True,
    help="Managed binary whose recorded version is burned.",
)
def _burn(generation: str, name: str) -> None:
    """Forbid promoting the version GENERATION records for a binary again."""
    bin_dir = native_bin_dir()
    with _reported():
        target = find_generation(bin_dir, generation)
        record = target.manifest["members"].get(name)
        if record is None:
            raise click.ClickException(f"generation {target.id} does not record {name}")
        version: str | None = record.get("version")
        if not version:
            raise click.ClickException(f"generation {target.id} records no version for {name}")
        burn_version(bin_dir, name, version, generation=target.id)
        click.echo(f"Burned {name} {version} from generation {target.id}.")


def _report_outcome(outcome: RestoreOutcome) -> None:
    click.echo(f"Restored generation {outcome.generation}; the daemon was started again.")
    for name, version in outcome.restored.items():
        click.echo(f"Restored {name} {version}.")
    for name, version in outcome.burned.items():
        click.echo(f"Burned the outgoing {name} {version}; it cannot be promoted again.")
    for file_name in outcome.moved_aside:
        click.echo(f"Moved aside the unverified file {file_name}.")


@rollback.command("restore")
@click.argument("generation")
@click.option(
    "--bin",
    "names",
    multiple=True,
    type=click.Choice(MANAGED_BINARIES),
    help="Member to restore; repeat for several. Default: gdaemon.",
)
@click.option("--all", "all_members", is_flag=True, help="Restore every member recorded.")
@click.option("--hold-token", "hold_token", default=None, help="Token of the hold, if any.")
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for protected cron runs and unresolved session handoffs before stopping.",
)
@click.pass_context
def _restore(
    ctx: click.Context,
    generation: str,
    names: tuple[str, ...],
    all_members: bool,
    hold_token: str | None,
    wait: bool,
) -> None:
    """Put a retained generation back as the installed binaries and restart the daemon."""
    from gobby.releases.activation import agent_activation_refusal

    if refusal := agent_activation_refusal("cutover"):
        raise click.ClickException(f"Refusing to restore: {refusal}")
    # A node never runs the Python daemon, so refuse before resolving anything.
    with contextlib.suppress(BootstrapConfigError):
        if refusal := node_daemon_refusal(load_bootstrap()):
            raise click.ClickException(refusal)
    if names and all_members:
        raise click.UsageError("--bin and --all cannot be combined")
    bin_dir = native_bin_dir()
    with _reported():
        target = find_generation(bin_dir, generation)
        members = restore_members(target, names=names, all_members=all_members)
    runtime = get_cli_runtime(ctx)
    expected_identity = target.manifest.get("schema_identity")
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
                raise RestoreError(f"daemon start failed (exit {exc.code})") from exc
        except click.ClickException as exc:
            raise RestoreError(exc.format_message()) from exc

    def start_refusal(candidate: Path | None) -> str | None:
        return restart_start_refusal(
            runtime, candidate, expected_identity=expected_identity
        ) or _stop_admission_refusal(ctx, wait=wait, logged_stale_markers=logged_stale_markers)

    with _reported():
        outcome = run_restore(
            bin_dir,
            target,
            members,
            hold_token=hold_token,
            stop_daemon=stop_daemon,
            start_daemon=start_daemon,
            start_refusal=start_refusal,
            verify_restart_target=_verify_restart_target,
        )
    _report_outcome(outcome)
