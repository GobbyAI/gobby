"""Schema-coherent promotion for workspace-built native binaries."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from gobby.install import bin_freshness_promotion
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.storage.schema_identity_pin import (
    SchemaIdentityError,
    stamp_bytes,
    validate_identity,
)
from gobby.utils.native_bin import native_bin_name

SET_MEMBERS = ("gcode", "gdaemon", "ghook", "gwiki")
IDENTITY_STAMP_NAME = ".gdaemon-schema-identity.json"
REBUILD_REMEDY = "rebuild and install all four together"
_PROBE_TIMEOUT_SECONDS = 10


class BinarySetCoherenceError(RuntimeError):
    """Raised when a workspace binary promotion would create a mixed set."""


def probe_set_member_identity(binary: Path, member: str) -> dict[str, int | str]:
    """Read one set member's embedded schema identity."""
    if member not in SET_MEMBERS:
        raise BinarySetCoherenceError(f"unknown workspace binary set member: {member}")
    arguments = (
        ("schema", "version", "--json") if member == "gdaemon" else ("schema-identity", "--json")
    )
    try:
        result = subprocess.run(  # nosec B603 - fixed arguments against a selected binary
            [str(binary), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BinarySetCoherenceError(
            f"{member} schema identity probe timed out after {_PROBE_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise BinarySetCoherenceError(
            f"failed to launch {member} schema identity probe: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise BinarySetCoherenceError(f"{member} schema identity probe failed: {detail}")
    try:
        parsed: object = json.loads(result.stdout)
        return validate_identity(parsed)
    except (json.JSONDecodeError, SchemaIdentityError) as exc:
        raise BinarySetCoherenceError(
            f"{member} schema identity probe returned an invalid contract: {exc}"
        ) from exc


def promote_workspace_binary_set(
    candidates: Mapping[str, Path],
    *,
    bin_dir: Path,
) -> None:
    """Promote a coherent partial or complete workspace binary set."""
    ordered = _validate_candidates(candidates)
    identities = {
        member: probe_set_member_identity(candidates[member], member) for member in ordered
    }
    complete_set = tuple(ordered) == SET_MEMBERS
    if complete_set:
        _require_candidate_agreement(identities)
    else:
        _require_installed_agreement(identities, bin_dir=bin_dir)

    bin_dir.mkdir(parents=True, exist_ok=True)
    promoted: list[str] = []
    try:
        with ExitStack() as locks:
            for member in ordered:
                lock = try_acquire_native_bin_lock(member, bin_dir=bin_dir)
                if lock is None:
                    raise OSError(f"{member} native binary update is already in progress")
                locks.enter_context(lock)
            for member in ordered:
                bin_freshness_promotion.stage_and_promote_binary_file(
                    candidates[member],
                    destination=bin_dir / native_bin_name(member),
                )
                promoted.append(member)
            if complete_set:
                _write_identity_stamp(bin_dir, identities[SET_MEMBERS[0]])
    except Exception as exc:
        unpromoted = [member for member in ordered if member not in promoted]
        raise BinarySetCoherenceError(
            "workspace binary set promotion failed; "
            f"promoted: {_member_list(promoted)}; "
            f"unpromoted: {_member_list(unpromoted)}; cause: {exc}"
        ) from exc


def _validate_candidates(candidates: Mapping[str, Path]) -> list[str]:
    if not candidates:
        raise BinarySetCoherenceError("workspace binary set promotion requires at least one member")
    unknown = sorted(set(candidates).difference(SET_MEMBERS))
    if unknown:
        raise BinarySetCoherenceError(f"unknown workspace binary set members: {', '.join(unknown)}")
    ordered = [member for member in SET_MEMBERS if member in candidates]
    for member in ordered:
        if not candidates[member].is_file():
            raise BinarySetCoherenceError(
                f"workspace binary set member {member} is missing: {candidates[member]}"
            )
    return ordered


def _require_candidate_agreement(identities: Mapping[str, dict[str, int | str]]) -> None:
    reference = identities[SET_MEMBERS[0]]
    if all(identity == reference for identity in identities.values()):
        return
    details = "; ".join(
        f"{member}={_render_identity(identities[member])}" for member in SET_MEMBERS
    )
    raise BinarySetCoherenceError(
        f"workspace binary set identities disagree: {details}; {REBUILD_REMEDY}"
    )


def _require_installed_agreement(
    identities: Mapping[str, dict[str, int | str]],
    *,
    bin_dir: Path,
) -> None:
    installed = _installed_identity(bin_dir)
    if installed is None:
        if not any((bin_dir / native_bin_name(member)).exists() for member in SET_MEMBERS):
            return
        raise BinarySetCoherenceError(
            "installed schema identity is unavailable for partial workspace promotion; "
            f"{REBUILD_REMEDY}"
        )
    mismatches = [member for member, identity in identities.items() if identity != installed]
    if not mismatches:
        return
    details = "; ".join(
        f"{member} embedded identity {_render_identity(identities[member])}, "
        f"installed identity {_render_identity(installed)}"
        for member in mismatches
    )
    raise BinarySetCoherenceError(
        f"workspace binary set promotion refused: {details}; {REBUILD_REMEDY}"
    )


def _installed_identity(bin_dir: Path) -> dict[str, int | str] | None:
    stamp = bin_dir / IDENTITY_STAMP_NAME
    if stamp.is_file():
        try:
            parsed: object = json.loads(stamp.read_text(encoding="utf-8"))
            return validate_identity(parsed)
        except (OSError, json.JSONDecodeError, SchemaIdentityError) as exc:
            raise BinarySetCoherenceError(
                f"installed schema identity pin is invalid: {exc}"
            ) from exc
    gdaemon = bin_dir / native_bin_name("gdaemon")
    if not gdaemon.is_file():
        return None
    return probe_set_member_identity(gdaemon, "gdaemon")


def _write_identity_stamp(bin_dir: Path, identity: dict[str, int | str]) -> None:
    fd, temporary = tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gdaemon-schema-identity-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fileobj:
            fileobj.write(stamp_bytes(identity))
            fileobj.flush()
            os.fsync(fileobj.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, bin_dir / IDENTITY_STAMP_NAME)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _render_identity(identity: Mapping[str, int | str]) -> str:
    return json.dumps(identity, separators=(",", ":"), sort_keys=True)


def _member_list(members: list[str]) -> str:
    return ", ".join(members) if members else "none"
