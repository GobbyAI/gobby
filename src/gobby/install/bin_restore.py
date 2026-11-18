"""Putting a retained generation back as the installed managed binaries.

A restore proves the generation restorable before anything stops, copies the members' retained
files out of the store, stops the daemon, and promotes those copies through the entry points a
workspace install uses. Those promotions retain the outgoing set, so a restore can itself be
undone by restoring the generation it replaced.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_generations import (
    GCLIENT_ALIAS,
    MANAGED_BINARIES,
    Generation,
    burn_version,
    installed_schema_identity,
    promotion_refusal,
    retention_refusal,
    sidecar_names,
)
from gobby.install.bin_release_identity import (
    BinaryReleaseVersionError,
    promotion_hold_token,
    release_identity_path,
)
from gobby.install.bin_set_coherence import BinarySetCoherenceError, promote_workspace_binary_set
from gobby.install.version_probe import probe_native_bin_version
from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name


class RestoreError(RuntimeError):
    """A restore cannot run, or failed after the daemon stopped; the message says what happened."""


@dataclass(frozen=True)
class RestoreOutcome:
    """What a restore put in place, the outgoing versions it burned, and the files it set aside."""

    generation: str
    restored: dict[str, str]
    burned: dict[str, str]
    moved_aside: list[str]


def restore_members(
    generation: Generation, *, names: Sequence[str] = (), all_members: bool = False
) -> list[str]:
    """The members a restore replaces: gdaemon by default, the named ones, or every recorded one."""
    recorded = generation.manifest["members"]
    if names and all_members:
        raise RestoreError("member names and all members cannot be combined")
    if all_members:
        return [name for name in MANAGED_BINARIES if name in recorded]
    if names:
        for name in names:
            if name not in recorded:
                raise RestoreError(f"{name} is not recorded in generation {generation.id}")
        return list(names)
    if "gdaemon" not in recorded:
        raise RestoreError(f"generation {generation.id} does not record gdaemon; name the members")
    return ["gdaemon"]


def restore_refusal(
    bin_dir: Path,
    generation: Generation,
    members: Sequence[str],
    *,
    hold_token: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Why restoring members from generation must not run, or None; writes nothing."""
    moment = _moment(now)
    for name in members:
        reason = _member_refusal(bin_dir, generation, name, hold_token, moment)
        if reason is not None:
            return reason
    schema = _schema_refusal(bin_dir, generation, members)
    if schema is not None:
        return schema
    return retention_refusal(bin_dir)


def run_restore(
    bin_dir: Path,
    generation: Generation,
    members: Sequence[str],
    *,
    hold_token: str | None = None,
    stop_daemon: Callable[[], bool],
    start_daemon: Callable[[], None],
    start_refusal: Callable[[Path | None], str | None],
    verify_restart_target: Callable[[Path], None] = lambda _bin_dir: None,
    now: datetime | None = None,
) -> RestoreOutcome:
    """Put the members of a generation back as the installed binaries and start the daemon."""
    moment = _moment(now)
    refusal = restore_refusal(bin_dir, generation, members, hold_token=hold_token, now=moment)
    if refusal is not None:
        raise RestoreError(f"refusing to restore: {refusal}")
    staging = _copy_out(generation, members)
    try:
        refusal = _release_refusal(bin_dir, generation, members, staging, hold_token)
        if refusal is not None:
            raise RestoreError(f"refusing to restore: {refusal}")
        candidate = staging / native_bin_name("gdaemon") if "gdaemon" in members else None
        refusal = start_refusal(candidate)
        if refusal is not None:
            raise RestoreError(f"refusing to restore: {refusal}")
        if not stop_daemon():
            raise RestoreError("the daemon did not stop, so nothing was replaced")
        return _replace_after_stop(
            bin_dir,
            generation,
            members,
            staging,
            hold_token=hold_token,
            start_daemon=start_daemon,
            verify_restart_target=verify_restart_target,
            moment=moment,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _moment(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now.astimezone(UTC)


def _recorded_files(record: dict[str, Any]) -> list[str]:
    files = record.get("files")
    return [name for name in files if isinstance(name, str)] if isinstance(files, list) else []


def _member_refusal(
    bin_dir: Path,
    generation: Generation,
    name: str,
    hold_token: str | None,
    moment: datetime,
) -> str | None:
    if name not in MANAGED_BINARIES:
        return f"{name} is not a managed binary"
    record = generation.manifest["members"].get(name)
    if not isinstance(record, dict):
        return f"generation {generation.id} does not record {name}"
    binary = generation.path / native_bin_name(name)
    for file_name in [native_bin_name(name), *_recorded_files(record)]:
        if not (generation.path / file_name).is_file():
            return f"generation {generation.id} is missing its retained {file_name}"
    if promotion.file_sha256(binary) != record.get("sha256"):
        return f"retained {name} in generation {generation.id} differs from its manifest sha256"
    version = record.get("version")
    if not isinstance(version, str) or not version:
        return f"generation {generation.id} records no version for {name}"
    probed = probe_native_bin_version(binary)
    if probed != version:
        return (
            f"retained {name} reports version {probed}, "
            f"but generation {generation.id} records {version}"
        )
    return promotion_refusal(bin_dir, name, version, hold_token, now=moment)


def _schema_refusal(bin_dir: Path, generation: Generation, members: Sequence[str]) -> str | None:
    """A part of the schema-coherent set needs a generation under the installed schema identity."""
    if not set(members) < set(SET_MEMBERS):
        return None
    if generation.manifest.get("schema_identity") == installed_schema_identity(bin_dir):
        return None
    return (
        f"generation {generation.id} was captured under another schema identity than the "
        f"installed set; restore with all members ({', '.join(SET_MEMBERS)}) together"
    )


def _copy_out(generation: Generation, members: Sequence[str]) -> Path:
    """Copy the members' retained files out of the store, so no later step reads from it."""
    staging = Path(tempfile.mkdtemp(prefix="gobby-restore-"))
    try:
        for name in members:
            record = generation.manifest["members"][name]
            for file_name in [native_bin_name(name), *_recorded_files(record)]:
                shutil.copy2(generation.path / file_name, staging / file_name)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _release_refusal(
    bin_dir: Path,
    generation: Generation,
    members: Sequence[str],
    staging: Path,
    hold_token: str | None,
) -> str | None:
    """What the promotion's release check would refuse after the stop, asked before it."""
    set_members = [name for name in SET_MEMBERS if name in members]
    groups = [set_members, *([name] for name in members if name not in SET_MEMBERS)]
    try:
        with promotion_hold_token(hold_token):
            for group in groups:
                # An installed member that reports no version is moved aside before promotion.
                candidates = {
                    name: staging / native_bin_name(name)
                    for name in group
                    if not _unverified(bin_dir / native_bin_name(name))
                }
                promotion.preflight_binary_promotions(
                    candidates,
                    bin_dir=bin_dir,
                    source_identities=_source_identities(generation, group),
                )
    except BinaryReleaseVersionError as exc:
        return str(exc)
    return None


def _unverified(installed: Path) -> bool:
    return installed.is_file() and probe_native_bin_version(installed) is None


def _replace_after_stop(
    bin_dir: Path,
    generation: Generation,
    members: Sequence[str],
    staging: Path,
    *,
    hold_token: str | None,
    start_daemon: Callable[[], None],
    verify_restart_target: Callable[[Path], None],
    moment: datetime,
) -> RestoreOutcome:
    """Replace the members with the copies, verify them, then start the daemon again."""
    replaced: list[str] = []
    outgoing: dict[str, str | None] = {}
    try:
        moved_aside = _move_aside_unverified(bin_dir, members, moment)
        outgoing = {name: _installed_version(bin_dir, name) for name in members}
        with promotion_hold_token(hold_token):
            _promote(bin_dir, generation, staging, members, replaced)
        restored = _verify_members(bin_dir, generation, members)
        burned = _burn_outgoing(bin_dir, generation, restored, outgoing, moment)
        if "gdaemon" in members:
            verify_restart_target(bin_dir)
        start_daemon()
        return RestoreOutcome(
            generation=generation.id,
            restored=restored,
            burned=burned,
            moved_aside=moved_aside,
        )
    except (OSError, RuntimeError) as exc:
        remaining = [name for name in members if name not in replaced]
        burn_note = _burn_swapped_over(bin_dir, generation, replaced, outgoing, moment)
        raise RestoreError(
            f"restore of generation {generation.id} failed after the daemon was stopped; "
            f"replaced: {_names(replaced)}; not replaced: {_names(remaining)}; cause: {exc}; "
            f"{burn_note}the daemon is stopped and was not restarted"
        ) from exc


def _move_aside_unverified(bin_dir: Path, members: Sequence[str], moment: datetime) -> list[str]:
    """Set aside each installed member that reports no version, under a dot-name."""
    moved: list[str] = []
    for name in members:
        installed = bin_dir / native_bin_name(name)
        if _unverified(installed):
            aside = f".{installed.name}.unverified-{moment:%Y%m%dT%H%M%S%fZ}"
            os.replace(installed, bin_dir / aside)
            moved.append(aside)
    return moved


def _installed_version(bin_dir: Path, name: str) -> str | None:
    installed = bin_dir / native_bin_name(name)
    return probe_native_bin_version(installed) if installed.is_file() else None


def _source_identities(generation: Generation, names: Sequence[str]) -> dict[str, str] | None:
    """Each named member's recorded source identity, or None when any of them has none."""
    identities: dict[str, str] = {}
    for name in names:
        identity = generation.manifest["members"][name].get("source_identity")
        if not isinstance(identity, str):
            return None
        identities[name] = identity
    return identities


def _promote(
    bin_dir: Path,
    generation: Generation,
    staging: Path,
    members: Sequence[str],
    replaced: list[str],
) -> None:
    """Promote the copies: the stamped-set members together, then each other member alone.

    Each promoted member's sidecars are restored as soon as its copy is in place, under the store
    lock, so a capture never pairs the restored bytes with the outgoing sidecars. A member whose
    sidecars do not settle is named in the error, and it still counts as replaced.
    """
    set_members = [name for name in SET_MEMBERS if name in members]
    if set_members:
        try:
            promote_workspace_binary_set(
                {name: staging / native_bin_name(name) for name in set_members},
                bin_dir=bin_dir,
                source_identities=_source_identities(generation, set_members),
                settle=partial(_restore_sidecars, bin_dir, generation, staging),
                # A ghook that predates --runtime-stamp has no document to capture, so nothing is
                # written for it here. Its own --version writes the stamp beside the install, and
                # _verify_members runs that after the swap.
                allow_unstamped_ghook=True,
            )
        except BinarySetCoherenceError as exc:
            # A set that fails part-way has already replaced the members it names, whether or not
            # their sidecars settled, so those members count as replaced.
            _mark_replaced(replaced, exc.promoted)
            raise
        _mark_replaced(replaced, set_members)
    for name in members:
        if name in SET_MEMBERS:
            continue
        source = staging / native_bin_name(name)
        identity = promotion.preflight_binary_promotions(
            {name: source},
            bin_dir=bin_dir,
            source_identities=_source_identities(generation, [name]),
        )[name]
        try:
            promotion.stage_and_promote_binary_file(
                source,
                destination=bin_dir / native_bin_name(name),
                identity=identity,
                after_swap=partial(
                    _restore_single_member, bin_dir, generation, staging, name, replaced
                ),
            )
        except promotion.BinaryStampsNotSettledError as exc:
            raise RestoreError(f"stamps unsettled for: {name}; cause: {exc.__cause__}") from exc


def _mark_replaced(replaced: list[str], names: Sequence[str]) -> None:
    """Add each of names not yet replaced, in the order given."""
    for name in names:
        if name not in replaced:
            replaced.append(name)


def _restore_single_member(
    bin_dir: Path,
    generation: Generation,
    staging: Path,
    name: str,
    replaced: list[str],
) -> None:
    """Runs once a single member's copy is swapped in: mark it replaced, then restore its sidecars.

    The swap has happened by the time this runs, so the member counts as replaced even if a
    sidecar write then fails.
    """
    _mark_replaced(replaced, [name])
    _restore_sidecars(bin_dir, generation, staging, name)


def _restore_sidecars(bin_dir: Path, generation: Generation, staging: Path, name: str) -> None:
    """Make a member's sidecars and gclient alias match the generation's copies."""
    record = generation.manifest["members"][name]
    recorded = _recorded_files(record)
    alias = native_bin_name(GCLIENT_ALIAS)
    receipt = release_identity_path(bin_dir, name).name
    # The promotion wrote the binary and receipt; the alias is handled below.
    promoted = {native_bin_name(name), receipt, alias}
    for file_name in recorded:
        if file_name not in promoted:
            _replace_with(staging / file_name, bin_dir / file_name)
    # The identity stamp belongs to the set promotion, so it is never removed here.
    for file_name in sidecar_names(name):
        if file_name not in recorded and file_name not in {receipt, IDENTITY_STAMP_NAME}:
            (bin_dir / file_name).unlink(missing_ok=True)
    if name == "gclient":
        if record.get("alias") is True:
            _replace_with(staging / alias, bin_dir / alias)
        else:
            (bin_dir / alias).unlink(missing_ok=True)


def _replace_with(source: Path, destination: Path) -> None:
    """Put a copy of source at destination with one rename, keeping source's mode."""
    fd, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}-", suffix=".tmp"
    )
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _verify_members(
    bin_dir: Path, generation: Generation, members: Sequence[str]
) -> dict[str, str]:
    """The version each member reports after the restore, which must be the recorded one."""
    restored: dict[str, str] = {}
    for name in members:
        version: str = generation.manifest["members"][name]["version"]
        probed = probe_native_bin_version(bin_dir / native_bin_name(name))
        if probed != version:
            raise RestoreError(f"{name} reports {probed} after the restore, not {version}")
        restored[name] = version
    return restored


def _burn_outgoing(
    bin_dir: Path,
    generation: Generation,
    restored: dict[str, str],
    outgoing: dict[str, str | None],
    moment: datetime,
) -> dict[str, str]:
    """Burn each replaced version that differs from the one restored, never the restored one."""
    burned: dict[str, str] = {}
    for name, version in restored.items():
        replaced_version = outgoing.get(name)
        if replaced_version is not None and replaced_version != version:
            burn_version(bin_dir, name, replaced_version, generation=generation.id, now=moment)
            burned[name] = replaced_version
    return burned


def _burn_swapped_over(
    bin_dir: Path,
    generation: Generation,
    replaced: Sequence[str],
    outgoing: dict[str, str | None],
    moment: datetime,
) -> str:
    """Burn the outgoing version of every member already swapped over, even when the restore fails.

    Returns a note for the RestoreError when a burn could not be written, or "" when all landed.
    """
    if not replaced:
        return ""
    restored = {name: generation.manifest["members"][name]["version"] for name in replaced}
    try:
        _burn_outgoing(bin_dir, generation, restored, outgoing, moment)
    except OSError as exc:
        return f"outgoing versions were not all burned: {exc}; "
    return ""


def _names(members: Sequence[str]) -> str:
    return ", ".join(members) if members else "none"
