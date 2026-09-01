"""Read-only comparison of the three schema heads a checkout can observe.

One installed ``~/.gobby/bin/gdaemon`` is shared by every checkout and worktree on
the machine, so the moment any branch adds a migration the other checkouts disagree
with it, and both can disagree with the live hub. Observability commands must report
that divergence instead of failing at the identity gate, which is exactly when the
divergence most needs seeing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gobby.install.bin_set_coherence import (
    IDENTITY_STAMP_NAME,
    SET_MEMBERS,
    BinarySetCoherenceError,
    probe_set_member_identity,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.schema_contract import SchemaContractError, expected_schema_identity
from gobby.storage.schema_identity_pin import SchemaIdentityError, validate_identity
from gobby.utils.native_bin import native_bin_dir, native_bin_name, resolve_native_bin

logger = logging.getLogger(__name__)

_VERSION_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SchemaHeads:
    """The checkout pin, the installed binary's embedded pin, and the live hub head."""

    checkout_version: int | None
    installed_version: int | None
    live_version: int | None

    @property
    def diverged(self) -> bool:
        """True when the heads that could be read do not all agree."""
        observed = {
            version
            for version in (self.checkout_version, self.installed_version, self.live_version)
            if version is not None
        }
        return len(observed) > 1

    def describe(self) -> str:
        """Render one dashboard line naming all three heads."""

        def render(version: int | None) -> str:
            return "unreadable" if version is None else f"v{version}"

        summary = (
            f"checkout pins {render(self.checkout_version)} · "
            f"installed gdaemon {render(self.installed_version)} · "
            f"live hub {render(self.live_version)}"
        )
        if self.diverged:
            return f"{summary} — DIVERGED"
        return summary


@dataclass(frozen=True)
class InstalledBinaryMismatch:
    """One installed set member whose embedded identity does not match the pin."""

    member: str
    identity: dict[str, int | str]


@dataclass(frozen=True)
class InstalledBinarySet:
    """Read-only coherence view of the managed native binary set."""

    pin_identity: dict[str, int | str] | None
    mismatches: tuple[InstalledBinaryMismatch, ...] = ()
    unreadable_members: tuple[str, ...] = ()
    pin_error: str | None = None

    @property
    def mixed(self) -> bool:
        return bool(self.mismatches or self.unreadable_members or self.pin_error)

    def describe(self) -> str:
        """Render the set mismatch with every affected member and identity."""
        details = [
            f"{mismatch.member} identity {_render_identity(mismatch.identity)}; "
            f"installed pin {_render_identity(self.pin_identity)}"
            for mismatch in self.mismatches
        ]
        details.extend(f"{member} identity unreadable" for member in self.unreadable_members)
        if self.pin_error is not None:
            details.append(self.pin_error)
        return "mixed installed binary set: " + "; ".join(details)


def collect_installed_binary_set(bin_dir: Path | None = None) -> InstalledBinarySet:
    """Probe installed set members and compare their identities with the pin."""
    root = native_bin_dir() if bin_dir is None else bin_dir
    installed = tuple(
        (member, root / native_bin_name(member))
        for member in SET_MEMBERS
        if (root / native_bin_name(member)).is_file()
    )
    if not installed:
        return InstalledBinarySet(pin_identity=None)

    stamp = root / IDENTITY_STAMP_NAME
    try:
        parsed: object = json.loads(stamp.read_text(encoding="utf-8"))
        pin = validate_identity(parsed)
    except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
        return InstalledBinarySet(
            pin_identity=None,
            pin_error=f"installed schema identity pin is unreadable: {exc}",
        )

    mismatches: list[InstalledBinaryMismatch] = []
    unreadable: list[str] = []
    for member, binary in installed:
        try:
            identity = probe_set_member_identity(binary, member)
        except BinarySetCoherenceError:
            logger.debug("Failed to probe installed %s schema identity", member, exc_info=True)
            unreadable.append(member)
            continue
        if identity != pin:
            mismatches.append(InstalledBinaryMismatch(member=member, identity=identity))
    return InstalledBinarySet(
        pin_identity=pin,
        mismatches=tuple(mismatches),
        unreadable_members=tuple(unreadable),
    )


def binary_set_apply_refusal(bin_dir: Path | None = None) -> str | None:
    """Return the installed-set divergence that makes start unsafe, if any."""
    view = collect_installed_binary_set(bin_dir)
    if not view.mixed:
        return None
    return f"{view.describe()}; rebuild and install all four together"


def _render_identity(identity: dict[str, int | str] | None) -> str:
    if identity is None:
        return "unreadable"
    contract = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"v{identity['latest_version']} {contract}"


def installed_schema_identity() -> dict[str, int | str] | None:
    """Read the installed gdaemon's embedded identity, or None when unreadable.

    ``gdaemon schema version`` deliberately does not enforce the expected identity,
    so this stays readable while the checkout pin and the binary disagree.
    """
    binary = resolve_native_bin("gdaemon")
    if binary is None:
        logger.debug("gdaemon is not installed; cannot read its embedded schema identity")
        return None
    try:
        result = subprocess.run(
            [binary, "schema", "version", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Failed to read installed gdaemon schema identity: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "gdaemon schema version exited %s: %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        return None
    try:
        return validate_identity(json.loads(result.stdout))
    except (json.JSONDecodeError, SchemaIdentityError) as exc:
        logger.debug("gdaemon schema version returned an unusable identity: %s", exc)
        return None


def live_schema_version(database: HubDatabase) -> int | None:
    """Read the live hub's applied schema head, or None when unreadable."""
    try:
        with database.transaction() as conn:
            row = conn.execute("SELECT MAX(version) AS head FROM schema_migrations").fetchone()
    except Exception as exc:
        logger.debug("Failed to read the live schema head: %s", exc)
        return None
    if row is None:
        return None
    value = row["head"]
    return value if isinstance(value, int) else None


def _checkout_version() -> int | None:
    try:
        value = expected_schema_identity()["latest_version"]
    except SchemaContractError as exc:
        logger.debug("Packaged schema identity is unreadable: %s", exc)
        return None
    return value if isinstance(value, int) else None


def collect_schema_heads(database: HubDatabase | None) -> SchemaHeads:
    """Gather every head that can be read without applying or verifying schema."""
    installed = installed_schema_identity()
    installed_version = installed["latest_version"] if installed is not None else None
    return SchemaHeads(
        checkout_version=_checkout_version(),
        installed_version=installed_version if isinstance(installed_version, int) else None,
        live_version=None if database is None else live_schema_version(database),
    )


def schema_apply_refusal(database: HubDatabase | None) -> str | None:
    """Name the divergence that would make a schema apply fail, or None.

    This is deliberately narrower than ``verify_schema``, which compares the live
    catalog against the packaged manifest and therefore fails on the ordinary upgrade
    path where the checkout adds a migration the hub has not applied yet. Only the two
    conditions no apply can resolve count: an installed binary whose embedded identity
    is not this checkout's, and a hub already ahead of this checkout. Anything
    unreadable is not a refusal — let the apply itself decide.
    """
    installed = installed_schema_identity()
    if installed is not None:
        try:
            expected = expected_schema_identity()
        except SchemaContractError as exc:
            logger.debug("Packaged schema identity is unreadable: %s", exc)
            return None
        if installed != expected:
            return (
                f"installed gdaemon carries schema identity v{installed['latest_version']} "
                f"({str(installed['latest_checksum'])[:12]}), which is not this checkout's "
                f"v{expected['latest_version']} ({str(expected['latest_checksum'])[:12]})"
            )

    checkout = _checkout_version()
    live = None if database is None else live_schema_version(database)
    if checkout is not None and live is not None and live > checkout:
        return f"live hub schema v{live} is newer than this checkout (v{checkout})"
    return None
