"""Schema-coherent promotion for workspace-built native binaries."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gobby.install import bin_freshness_promotion
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import managed_bin_specs
from gobby.install.bin_generations import (
    MANAGED_BINARIES,
    GenerationStoreError,
    promotion_guard,
    promotion_refusal,
    record_promotion,
)
from gobby.install.bin_release_identity import (
    BinaryReleaseIdentity,
    BinaryReleaseVersionError,
    current_hold_token,
)
from gobby.install.version_probe import probe_native_bin_version
from gobby.storage.schema_identity_pin import (
    SchemaIdentityError,
    stamp_bytes,
    validate_identity,
)
from gobby.utils import spawn
from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

logger = logging.getLogger(__name__)

REBUILD_REMEDY = "rebuild and install all three together"
_PROBE_TIMEOUT_SECONDS = 10
GHOOK = "ghook"
RUNTIME_STAMP_NAME = ".ghook-runtime.json"
_RUNTIME_STAMP_KEYS = frozenset({"ghook_version", "response_capability", "schema_version"})


class BinarySetCoherenceError(RuntimeError):
    """Raised when a workspace binary promotion would create a mixed set.

    `promoted` names the members whose new bytes are already in place when the error is raised,
    so a caller can tell which members a failed set promotion actually replaced.
    """

    def __init__(self, message: str, *, promoted: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.promoted: tuple[str, ...] = tuple(promoted)


class RuntimeStampUnsupportedError(BinarySetCoherenceError):
    """A ghook candidate prints no runtime stamp: --runtime-stamp exits non-zero or prints none."""


def workspace_build_only(member: str) -> str:
    """Refusal for a set-member install that cannot use its only source."""
    return (
        f"{member} installs only from the Gobby workspace build; run `gobby install` "
        "from a Gobby source checkout with cargo on PATH"
    )


@dataclass(frozen=True, slots=True)
class WorkspacePromotionMetadata:
    """Install provenance written after a complete workspace set promotes."""

    install_method: str
    target: str


def probe_set_member_identity(binary: Path, member: str) -> dict[str, int | str]:
    """Read one set member's embedded schema identity."""
    if member not in SET_MEMBERS:
        raise BinarySetCoherenceError(f"unknown workspace binary set member: {member}")
    arguments = (
        ("schema", "version", "--json") if member == "gdaemon" else ("schema-identity", "--json")
    )
    try:
        result = spawn.run(
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
    metadata: WorkspacePromotionMetadata | None = None,
    workspace_root: Path | None = None,
    source_identities: Mapping[str, str] | None = None,
    prepare_staged: Mapping[str, Callable[[Path], None]] | None = None,
    settle: Callable[[str], None] | None = None,
    after_swap: Callable[[], None] | None = None,
    allow_unstamped_ghook: bool = False,
) -> None:
    """Promote a coherent partial or complete workspace binary set.

    Each member is copied into staging and codesigned there, with `prepare_staged[member]` run
    on the copy after that, before the store guard is taken. The guard covers the swaps, the
    settling of each promoted member, the identity stamp, the ghook runtime stamp, and
    `after_swap`. Settling a member writes its metadata when `metadata` is given, then calls
    `settle(member)`; it runs for every member after the swaps, and for the members already
    swapped when the set fails part-way. A failure while swapping raises BinarySetCoherenceError
    naming what was promoted. A failure to settle names the members whose stamps are unsettled.
    Either way the swapped members are recorded, and the original exception is the __cause__.

    A ghook member's runtime stamp is the document its staged copy prints for --runtime-stamp.
    It is captured before the guard, and a copy that cannot print one is refused before any swap.
    With `allow_unstamped_ghook`, such a copy is promoted and no stamp is written for it.
    """
    ordered = _validate_candidates(candidates)
    identities = {
        member: probe_set_member_identity(candidates[member], member) for member in ordered
    }
    complete_set = tuple(ordered) == SET_MEMBERS
    if metadata is not None and not complete_set:
        raise BinarySetCoherenceError(
            "workspace promotion metadata requires all three binary set members"
        )
    if complete_set:
        _require_candidate_agreement(identities)
    else:
        _require_installed_agreement(identities, bin_dir=bin_dir)

    releases = bin_freshness_promotion.preflight_binary_promotions(
        dict(candidates),
        bin_dir=bin_dir,
        workspace_root=workspace_root,
        source_identities=source_identities,
    )
    source_identities = {name: release.source_identity for name, release in releases.items()}

    bin_dir.mkdir(parents=True, exist_ok=True)
    # Read before anything is replaced, so a commit landing mid-promotion is not recorded.
    source_commit = _head_commit(workspace_root)
    promoted: list[str] = []
    runtime_stamps: dict[str, bytes] = {}
    try:
        with ExitStack() as locks:
            for member in ordered:
                lock = try_acquire_native_bin_lock(member, bin_dir=bin_dir)
                if lock is None:
                    raise OSError(f"{member} native binary update is already in progress")
                locks.enter_context(lock)
            releases = bin_freshness_promotion.preflight_binary_promotions(
                dict(candidates),
                bin_dir=bin_dir,
                workspace_root=workspace_root,
                source_identities=source_identities,
            )
            with ExitStack() as staging:
                # Every member is staged, codesigned and probed before the store lock is taken.
                staged = {
                    member: staging.enter_context(
                        bin_freshness_promotion.staged_candidate(
                            candidates[member],
                            bin_dir=bin_dir,
                            binary_name=native_bin_name(member),
                            prepare_staged=_member_preparer(
                                member,
                                prepare_staged,
                                releases=releases,
                                runtime_stamps=runtime_stamps,
                                allow_unstamped_ghook=allow_unstamped_ghook,
                            ),
                        )
                    )
                    for member in ordered
                }
                versions = _staged_versions(staged) if complete_set and metadata is not None else {}
                with promotion_guard(bin_dir, MANAGED_BINARIES):
                    try:
                        _require_promotable(releases, ordered, bin_dir=bin_dir)
                        try:
                            for member in ordered:
                                bin_freshness_promotion.swap_staged_binary(
                                    staged[member],
                                    bin_dir=bin_dir,
                                    binary_name=native_bin_name(member),
                                    identity=releases[member],
                                )
                                promoted.append(member)
                        except Exception as swap_error:
                            # The members already in place are settled before the set fails.
                            _settle_promoted(
                                bin_dir,
                                promoted,
                                metadata=metadata,
                                versions=versions,
                                settle=settle,
                                swap_error=swap_error,
                            )
                            raise
                        try:
                            _settle_promoted(
                                bin_dir,
                                promoted,
                                metadata=metadata,
                                versions=versions,
                                settle=settle,
                            )
                            if complete_set:
                                _write_identity_stamp(bin_dir, identities[SET_MEMBERS[0]])
                        finally:
                            # The settle may copy or remove the runtime stamp, so the captured
                            # document is written after it, and also when the settle fails:
                            # the swapped ghook is the one the stamp must describe.
                            if GHOOK in promoted and GHOOK in runtime_stamps:
                                write_atomic_bytes(
                                    bin_dir / RUNTIME_STAMP_NAME,
                                    runtime_stamps[GHOOK],
                                    mode=0o644,
                                )
                        if after_swap is not None:
                            after_swap()
                    finally:
                        # Recorded whatever happened after the swaps: the members already in
                        # place are installed, so the record must say so.
                        _record_promotion(bin_dir, promoted, source_commit)
    except Exception as exc:
        if promoted and len(promoted) == len(ordered):
            subject = f"workspace binary set (promoted: {_member_list(promoted)})"
            raise BinarySetCoherenceError(
                bin_freshness_promotion.stamps_not_settled_message(subject, exc),
                promoted=promoted,
            ) from exc
        unpromoted = [member for member in ordered if member not in promoted]
        raise BinarySetCoherenceError(
            "workspace binary set promotion failed; "
            f"promoted: {_member_list(promoted)}; "
            f"unpromoted: {_member_list(unpromoted)}; cause: {exc}",
            promoted=promoted,
        ) from exc


def _member_preparer(
    member: str,
    prepare_staged: Mapping[str, Callable[[Path], None]] | None,
    *,
    releases: Mapping[str, BinaryReleaseIdentity],
    runtime_stamps: dict[str, bytes],
    allow_unstamped_ghook: bool,
) -> Callable[[Path], None]:
    """Codesign a member's staged copy, capture a ghook's stamp, then run the caller's hook.

    The stamp is captured from the signed copy, because that copy is the one swapped in.
    """
    caller_hook = prepare_staged.get(member) if prepare_staged is not None else None

    def prepare(staged: Path) -> None:
        _codesign_workspace_binary(staged)
        if member == GHOOK:
            try:
                runtime_stamps[member] = _capture_runtime_stamp(
                    staged, version=releases[member].version
                )
            except RuntimeStampUnsupportedError:
                if not allow_unstamped_ghook:
                    raise
        if caller_hook is not None:
            caller_hook(staged)

    return prepare


def _capture_runtime_stamp(candidate: Path, *, version: str) -> bytes:
    """The runtime stamp a staged ghook prints for --runtime-stamp, exactly as it is printed.

    The stamp must name the version being installed. A copy that exits non-zero or prints no
    stamp document raises RuntimeStampUnsupportedError; its --version is never run instead.
    """
    try:
        result = spawn.run(
            [str(candidate), "--runtime-stamp"],
            check=False,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BinarySetCoherenceError(
            f"ghook --runtime-stamp timed out after {_PROBE_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise BinarySetCoherenceError(f"failed to launch ghook --runtime-stamp: {exc}") from exc
    stamp_version = _stamp_version(result.stdout) if result.returncode == 0 else None
    if stamp_version is None:
        raise RuntimeStampUnsupportedError(
            f"ghook does not support --runtime-stamp (exit {result.returncode}); "
            "rebuild ghook from this workspace"
        )
    if not _same_version(stamp_version, version):
        raise BinarySetCoherenceError(
            f"ghook --runtime-stamp reports {stamp_version}, but the candidate reports {version}"
        )
    return bytes(result.stdout)


def _stamp_version(payload: bytes) -> str | None:
    """The ghook_version of a runtime stamp document, or None when payload is not one."""
    try:
        parsed: object = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != _RUNTIME_STAMP_KEYS:
        return None
    version = parsed["ghook_version"]
    return version if isinstance(version, str) else None


def _same_version(reported: str, version: str) -> bool:
    """Whether a stamp's version matches the candidate's, ignoring any -pre or +build suffix."""
    return reported == version or reported.startswith((f"{version}-", f"{version}+"))


def _staged_versions(staged: Mapping[str, Path]) -> dict[str, str]:
    """Each staged member's reported version, probed before the swap so the stamps match it."""
    versions: dict[str, str] = {}
    for member, path in staged.items():
        version = probe_native_bin_version(path)
        if version is None:
            raise BinarySetCoherenceError(f"{member} did not report a version")
        versions[member] = version
    return versions


def _require_promotable(
    releases: Mapping[str, BinaryReleaseIdentity], ordered: list[str], *, bin_dir: Path
) -> None:
    """Refuse the set before any member is replaced if a candidate is held or burned."""
    token = current_hold_token()
    for member in ordered:
        refusal = promotion_refusal(bin_dir, member, releases[member].version, token)
        if refusal is not None:
            raise BinaryReleaseVersionError(f"{member}: {refusal}")


def _record_promotion(bin_dir: Path, promoted: list[str], source_commit: str | None) -> None:
    """Note the promoted set with its source commit; a failure here must not undo it."""
    try:
        record_promotion(bin_dir, promoted, source_commit=source_commit)
    except GenerationStoreError:
        logger.warning(
            "binary set was promoted but its installed record was not written", exc_info=True
        )


def _head_commit(workspace_root: Path | None) -> str | None:
    """The commit the workspace has checked out; None without a workspace or a commit."""
    if workspace_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _codesign_workspace_binary(binary: Path) -> None:
    """Ad-hoc sign a staged workspace binary before inode promotion on macOS."""
    if sys.platform != "darwin" or shutil.which("codesign") is None:
        return
    try:
        result = spawn.run(
            ["codesign", "-f", "-s", "-", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise BinarySetCoherenceError(
            f"{binary.name} ad-hoc signing timed out after 30 seconds"
        ) from exc
    except OSError as exc:
        raise BinarySetCoherenceError(
            f"{binary.name} ad-hoc signing could not start: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise BinarySetCoherenceError(f"{binary.name} ad-hoc signing failed: {detail}")


def _settle_promoted(
    bin_dir: Path,
    promoted: Sequence[str],
    *,
    metadata: WorkspacePromotionMetadata | None,
    versions: Mapping[str, str],
    settle: Callable[[str], None] | None,
    swap_error: Exception | None = None,
) -> None:
    """Settle each promoted member in order; a failure names the members left unsettled.

    A member's new bytes are already in place, so a failure here never undoes its swap. When the
    set failed part-way, `swap_error` is named too, so the cause of that failure is not lost.
    """
    for index, member in enumerate(promoted):
        try:
            if metadata is not None:
                _write_member_metadata(bin_dir, metadata, member, versions[member])
            if settle is not None:
                settle(member)
        except Exception as exc:
            note = f"; set failed first: {swap_error}" if swap_error is not None else ""
            raise BinarySetCoherenceError(
                f"stamps unsettled for: {_member_list(list(promoted[index:]))}{note}; cause: {exc}",
                promoted=promoted,
            ) from exc


def _write_member_metadata(
    bin_dir: Path, metadata: WorkspacePromotionMetadata, member: str, version: str
) -> None:
    """Write one member's stamp and install sidecar; the version was probed before the swap."""
    installed_at = datetime.now(UTC).isoformat()
    spec = {candidate.name: candidate for candidate in managed_bin_specs()}[member]
    sidecar = {
        "install_method": metadata.install_method,
        "install_source_url": None,
        "installed_version": version,
        "installed_at": installed_at,
        "target": metadata.target,
    }
    write_atomic_bytes(bin_dir / spec.stamp_name, f"{version}\n".encode(), mode=0o644)
    write_atomic_bytes(
        bin_dir / spec.sidecar_name,
        (json.dumps(sidecar, sort_keys=True) + "\n").encode(),
        mode=0o644,
    )


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
    write_atomic_bytes(bin_dir / IDENTITY_STAMP_NAME, stamp_bytes(identity), mode=0o644)


def write_atomic_bytes(path: Path, content: bytes, *, mode: int) -> None:
    """Replace path with content in one rename, so no reader sees a truncated file."""
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fileobj:
            fileobj.write(content)
            fileobj.flush()
            os.fsync(fileobj.fileno())
            os.fchmod(fileobj.fileno(), mode)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _render_identity(identity: Mapping[str, int | str]) -> str:
    return json.dumps(identity, separators=(",", ":"), sort_keys=True)


def _member_list(members: list[str]) -> str:
    return ", ".join(members) if members else "none"
