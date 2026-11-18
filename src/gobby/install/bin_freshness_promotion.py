"""Staged, atomic promotion helpers for managed native binaries."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess  # nosec B404 # reads git commit times for staleness reporting
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import IO

from gobby.install.bin_freshness_github import SourceUnavailableError
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_generations import (
    MANAGED_BINARIES,
    GenerationStoreError,
    promotion_guard,
    promotion_refusal,
    record_promotion,
    retain_outgoing,
)
from gobby.install.bin_release_identity import (
    RELEASE_CRATES,
    BinaryReleaseIdentity,
    BinaryReleaseVersionError,
    check_binary_release,
    current_hold_token,
    release_identity_path,
    write_release_identity,
)
from gobby.utils import spawn
from gobby.utils.native_bin import native_bin_dir

logger = logging.getLogger(__name__)

NATIVE_BINARY_MODE = 0o755


class BinaryStampsNotSettledError(RuntimeError):
    """A binary was swapped in, but the stamps and sidecars that describe it did not settle.

    The new bytes are installed and the promotion is recorded. The hook's error is the cause.
    Callers report the install as replaced with its stamps unsettled, and write no stamp.
    """


def stamps_not_settled_message(subject: str, cause: BaseException) -> str:
    """The one wording for an installed binary whose stamps did not settle, on every path."""
    return (
        f"{subject} was installed but its stamps were not settled; "
        f"rerun the install; cause: {cause}"
    )


def stage_and_promote_release_binary(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    bin_dir: Path,
    asset_name: str,
    prepare_staged: Callable[[Path], None] | None = None,
    after_swap: Callable[[], None] | None = None,
) -> None:
    """Extract a release binary to staging and atomically promote it.

    `prepare_staged` and `after_swap` follow the rules of stage_and_promote_binary_file.
    """

    def write_staged(staged_binary: Path) -> None:
        _extract_binary(
            archive_bytes,
            archive_ext=archive_ext,
            binary_name=binary_name,
            destination=staged_binary,
            asset_name=asset_name,
        )
        if prepare_staged is not None:
            prepare_staged(staged_binary)

    _stage_and_promote(
        binary_name, bin_dir=bin_dir, write_staged=write_staged, after_swap=after_swap
    )


def stage_and_promote_binary_file(
    source: Path,
    *,
    destination: Path,
    prepare_staged: Callable[[Path], None] | None = None,
    workspace_root: Path | None = None,
    identity: BinaryReleaseIdentity | None = None,
    after_swap: Callable[[], None] | None = None,
) -> None:
    """Copy a local binary to staging and atomically promote it.

    `prepare_staged` runs on the staged copy before the swap, with no store lock held, so
    codesigning and version probes belong there. `after_swap` runs once the new bytes are in
    place and before the promotion is recorded. For a managed member it runs under the store
    lock, so it should only write sidecars. A failure in it raises BinaryStampsNotSettledError
    with the hook's error as the cause; the new bytes stay installed.
    """

    name = destination.stem if destination.suffix == ".exe" else destination.name
    identities = (
        {name: identity}
        if identity is not None
        else preflight_binary_promotions(
            {name: source}, bin_dir=destination.parent, workspace_root=workspace_root
        )
    )

    def write_staged(staged_binary: Path) -> None:
        with source.open("rb") as fileobj:
            _write_staged_binary(staged_binary, fileobj)
        if prepare_staged is not None:
            prepare_staged(staged_binary)

    _stage_and_promote(
        destination.name,
        bin_dir=destination.parent,
        write_staged=write_staged,
        identity=identities.get(name),
        after_swap=after_swap,
    )


def install_staged_cargo_binary(
    command: list[str],
    *,
    binary_name: str,
    bin_dir: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int,
    prepare_staged: Callable[[Path], None] | None = None,
    after_swap: Callable[[], None] | None = None,
) -> bool:
    """Install a Cargo artifact in isolation and guard its atomic promotion.

    `prepare_staged` and `after_swap` follow the rules of stage_and_promote_binary_file.
    """
    with tempfile.TemporaryDirectory(prefix=f"{binary_name}-cargo-") as temporary:
        install_root = Path(temporary)
        staged_command = command.copy()
        if "--root" in staged_command:
            staged_command[staged_command.index("--root") + 1] = str(install_root)
            source = install_root / "bin" / binary_name
        else:
            staged_command[staged_command.index("--install-path") + 1] = str(install_root)
            source = install_root / binary_name
        result = run(staged_command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return False

        def write_staged(staged_binary: Path) -> None:
            with source.open("rb") as fileobj:
                _write_staged_binary(staged_binary, fileobj)
            if prepare_staged is not None:
                prepare_staged(staged_binary)

        name = Path(binary_name).stem if binary_name.endswith(".exe") else binary_name
        lock = try_acquire_native_bin_lock(name, bin_dir=bin_dir)
        if lock is None:
            return False
        with lock:
            _stage_and_promote(
                binary_name, bin_dir=bin_dir, write_staged=write_staged, after_swap=after_swap
            )
        return True


def preflight_binary_promotions(
    candidates: dict[str, Path],
    *,
    bin_dir: Path,
    workspace_root: Path | None = None,
    source_identities: Mapping[str, str] | None = None,
) -> dict[str, BinaryReleaseIdentity]:
    """Check every workspace release before any member or daemon is changed."""
    managed = {name: source for name, source in candidates.items() if name in RELEASE_CRATES}
    if not managed:
        return {}
    if workspace_root is None:
        crates_root = _workspace_crates_root()
        if crates_root is None:
            raise BinaryReleaseVersionError(
                "workspace release source identity unavailable; use a checkout"
            )
        workspace_root = crates_root.parent
    return {
        name: check_binary_release(
            name,
            source,
            bin_dir=bin_dir,
            workspace_root=workspace_root,
            source_identity=source_identities[name] if source_identities is not None else None,
        )
        for name, source in managed.items()
    }


def _member_name(binary_name: str) -> str:
    return Path(binary_name).stem if binary_name.endswith(".exe") else binary_name


@contextmanager
def _staged(
    binary_name: str, *, bin_dir: Path, write_staged: Callable[[Path], None]
) -> Iterator[Path]:
    """Write a candidate into a private staging directory beside bin_dir and remove it on exit."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{binary_name}-staging-", dir=str(bin_dir)))
    try:
        staged_binary = staging_dir / binary_name
        write_staged(staged_binary)
        yield staged_binary
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


@contextmanager
def staged_candidate(
    source: Path,
    *,
    bin_dir: Path,
    binary_name: str,
    prepare_staged: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    """Copy source into staging and prepare the copy, holding no store lock.

    Pair with swap_staged_binary to install it. A set promotion stages every member through
    this before it takes the store guard, so codesigning and probes never run under the lock.
    """

    def write_staged(staged_binary: Path) -> None:
        with source.open("rb") as fileobj:
            _write_staged_binary(staged_binary, fileobj)
        if prepare_staged is not None:
            prepare_staged(staged_binary)

    with _staged(binary_name, bin_dir=bin_dir, write_staged=write_staged) as staged_binary:
        yield staged_binary


def _stage_and_promote(
    binary_name: str,
    *,
    bin_dir: Path,
    write_staged: Callable[[Path], None],
    identity: BinaryReleaseIdentity | None = None,
    after_swap: Callable[[], None] | None = None,
) -> None:
    with _staged(binary_name, bin_dir=bin_dir, write_staged=write_staged) as staged_binary:
        name = _member_name(binary_name)
        if name in RELEASE_CRATES and identity is None:
            identity = check_binary_release(
                name, staged_binary, bin_dir=bin_dir, workspace_root=None
            )
        swap_staged_binary(
            staged_binary,
            bin_dir=bin_dir,
            binary_name=binary_name,
            identity=identity,
            after_swap=after_swap,
        )


def swap_staged_binary(
    staged_binary: Path,
    *,
    bin_dir: Path,
    binary_name: str,
    identity: BinaryReleaseIdentity | None,
    after_swap: Callable[[], None] | None = None,
) -> None:
    """Swap a staged candidate in as binary_name, then run after_swap and record the promotion.

    A managed member is swapped, settled and recorded under the store lock, so a capture never
    pairs the new bytes with the outgoing sidecars. The record is written even when after_swap
    fails, because the new bytes are already in place; that failure then raises
    BinaryStampsNotSettledError with the hook's error as its cause. A caller already holding the
    store guard re-enters it.
    """
    name = _member_name(binary_name)
    if name not in MANAGED_BINARIES:
        _swap_in(
            staged_binary, bin_dir=bin_dir, binary_name=binary_name, name=name, identity=identity
        )
        # Only managed members are captured by the generation store, so a non-member's
        # post-swap writes have no capture to race and need no store lock.
        if after_swap is not None:
            _settle_after_swap(name, after_swap)
        return
    # The empty guard only holds the store lock across the swap; retention stays an explicit
    # call here, under that lock, so this module keeps its retention seam.
    with promotion_guard(bin_dir, []):
        retain_outgoing(bin_dir, [name])
        _swap_in(
            staged_binary, bin_dir=bin_dir, binary_name=binary_name, name=name, identity=identity
        )
        try:
            if after_swap is not None:
                _settle_after_swap(name, after_swap)
        finally:
            _record_promotion(bin_dir, name)


def _settle_after_swap(name: str, after_swap: Callable[[], None]) -> None:
    """Run a post-swap hook. Its failure leaves the new bytes installed, so it is typed as such."""
    try:
        after_swap()
    except Exception as exc:
        raise BinaryStampsNotSettledError(stamps_not_settled_message(name, exc)) from exc


def _swap_in(
    staged_binary: Path,
    *,
    bin_dir: Path,
    binary_name: str,
    name: str,
    identity: BinaryReleaseIdentity | None,
) -> None:
    """Replace the installed binary unless a hold or burn set since preflight refuses it."""
    refusal = promotion_refusal(
        bin_dir, name, identity.version if identity is not None else None, current_hold_token()
    )
    if refusal is not None:
        raise BinaryReleaseVersionError(f"{name}: {refusal}")
    # The outgoing receipt describes the bytes this swap replaces. Drop it first, so a failed
    # receipt write after the swap cannot leave it naming the old version beside the new bytes.
    receipt = release_identity_path(bin_dir, name)
    outgoing_receipt = receipt.read_bytes() if receipt.is_file() else None
    receipt.unlink(missing_ok=True)
    try:
        os.replace(staged_binary, bin_dir / binary_name)
    except BaseException as replace_exc:
        # The swap did not happen, so the installed binary is still the outgoing one.
        if outgoing_receipt is not None:
            try:
                _restore_receipt(receipt, outgoing_receipt)
            except OSError as restore_exc:
                # The installed binary is the outgoing one with no receipt, so the loss is named.
                raise OSError(
                    f"{name}: the swap failed and the outgoing release receipt {receipt.name} "
                    f"could not be put back, so it is lost: {restore_exc}"
                ) from replace_exc
        raise
    if identity is not None:
        _record_receipt(bin_dir, name, identity)


def _restore_receipt(path: Path, payload: bytes) -> None:
    """Put an outgoing receipt back byte-for-byte; a failure raises for the caller to name it."""
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise


def _record_receipt(bin_dir: Path, name: str, identity: BinaryReleaseIdentity) -> None:
    """Note the release receipt of a binary already in place; a failure here must not undo it."""
    try:
        write_release_identity(bin_dir, name, identity)
    except OSError:
        logger.warning(
            "%s was promoted but its release receipt was not written", name, exc_info=True
        )


def _record_promotion(bin_dir: Path, name: str) -> None:
    """Note a promotion whose binary is already in place; a failure here must not undo it."""
    try:
        record_promotion(bin_dir, [name])
    except GenerationStoreError:
        logger.warning(
            "%s was promoted but its installed record was not written", name, exc_info=True
        )


def _extract_binary(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    destination: Path,
    asset_name: str,
) -> None:
    try:
        if archive_ext == "zip":
            with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
                for member_name in archive.namelist():
                    if member_name.endswith(f"/{binary_name}") or member_name == binary_name:
                        with archive.open(member_name) as fileobj:
                            _write_staged_binary(destination, fileobj)
                        return
        else:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.name.endswith(f"/{binary_name}") or member.name == binary_name:
                        extracted_file = archive.extractfile(member)
                        if extracted_file is None:
                            continue
                        with extracted_file:
                            _write_staged_binary(destination, extracted_file)
                        return
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SourceUnavailableError(f"{asset_name}: extraction failed: {exc}") from exc
    raise SourceUnavailableError(f"{asset_name}: binary {binary_name} not found")


def _write_staged_binary(path: Path, source: IO[bytes]) -> None:
    with path.open("wb") as fileobj:
        shutil.copyfileobj(source, fileobj)
        fileobj.flush()
        os.fsync(fileobj.fileno())
        os.fchmod(fileobj.fileno(), NATIVE_BINARY_MODE)


# Unpublished managed binaries (gterm, gclient) carry a static crate version, so
# their version stamp can never report staleness. The only honest freshness test
# is the content of the artifact the workspace build just produced, recorded here
# beside the binary so the next install can compare without promoting. A build
# that confirms the installed binary current re-records the stamp, so its mtime
# is the last time the installed bytes were known to match the source.
SOURCE_HASH_SUFFIX = "-source-sha256"


def file_sha256(path: Path) -> str:
    """Return the hex sha256 of a file, reading it in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash_path(bin_dir: Path, name: str) -> Path:
    """Path of the recorded build-artifact hash for a managed binary."""
    return bin_dir / f".{name}{SOURCE_HASH_SUFFIX}"


def read_source_hash(bin_dir: Path, name: str) -> str | None:
    """Read the recorded build-artifact hash, or None when absent or unreadable."""
    path = source_hash_path(bin_dir, name)
    try:
        if not path.exists():
            return None
        return path.read_text().strip() or None
    except OSError:
        return None


def write_source_hash(bin_dir: Path, name: str, digest: str) -> None:
    """Record the build-artifact hash atomically beside the promoted binary."""
    target = source_hash_path(bin_dir, name)
    fd, tmp_path = tempfile.mkstemp(dir=str(bin_dir), prefix=f".{name}-sha-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def clear_source_hash(bin_dir: Path, name: str) -> None:
    """Forget the recorded hash so the next workspace build always promotes."""
    source_hash_path(bin_dir, name).unlink(missing_ok=True)


def workspace_binary_is_current(
    bin_dir: Path, name: str, built: Path, installed: Path, *, workspace_root: Path | None = None
) -> bool:
    """True when the installed binary already came from this exact build artifact.

    A True answer re-records the stamp so its mtime says when the installed bytes
    were last confirmed against a build of the source. The signed binary itself is
    never touched: a new inode is what a real promotion means.
    """
    if not installed.exists():
        return False
    recorded = read_source_hash(bin_dir, name)
    if recorded is None or recorded != file_sha256(built):
        return False
    preflight_binary_promotions({name: built}, bin_dir=bin_dir, workspace_root=workspace_root)
    try:
        # No swap happens here, so the stamp takes the store lock itself.
        with promotion_guard(bin_dir, []):
            write_source_hash(bin_dir, name, recorded)
    except OSError:
        # The answer stands; only the evidence for the status marker is lost.
        logger.warning(
            "%s: installed binary is current but its source-hash stamp could not be "
            "re-recorded; status may keep reporting it stale",
            name,
            exc_info=True,
        )
    return True


# Which crates a binary is built from. `workspace_binary_is_current` answers a
# sharper question but needs a build artifact to compare against, so it cannot
# tell an operator their installed binary is behind the source it was built from.
# gclient and gmcp carry gcore because they depend on it: a gobby-core change
# leaves an unrebuilt gclient or gmcp just as stale as one of its own. gterm has
# no gobby-core dependency, so it watches only itself.
WORKSPACE_BINARY_CRATES: dict[str, tuple[str, ...]] = {
    "gclient": ("gclient", "gcore"),
    "gmcp": ("gmcp", "gcore"),
    "gterm": ("gterminal",),
}
_SOURCE_COMMIT_TIMEOUT_SECONDS = 10


def _workspace_crates_root() -> Path | None:
    """The checkout's `crates/` directory, or None when running from a release."""
    for parent in Path(__file__).resolve().parents:
        crates = parent / "crates"
        if (parent / "Cargo.toml").is_file() and crates.is_dir():
            return crates
    return None


def last_source_commit_time(crates_root: Path, crates: tuple[str, ...]) -> float | None:
    """Unix time of the newest commit touching any of *crates*, or None.

    Git rather than a source-tree walk: `rglob` over gclient and gcore stats 360
    files and costs ~270ms, which is far too much for a status payload the web UI
    polls. This is ~20ms and in line with the version probes already in that
    payload. It trades away uncommitted edits, which is the right trade -- an
    operator editing a crate knows they are editing it. The silent case is a
    commit that landed and was never rebuilt.
    """
    paths = [str(crates_root / crate) for crate in crates]
    try:
        result = spawn.run(
            ["git", "log", "-1", "--format=%ct", "--", *paths],
            cwd=crates_root.parent,
            capture_output=True,
            text=True,
            timeout=_SOURCE_COMMIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    stamp = result.stdout.strip()
    try:
        return float(stamp)
    except ValueError:
        return None


def native_bin_predates_source(name: str, *, bin_dir: Path | None = None) -> bool | None:
    """True when `name`'s installed binary predates the last commit to its crates.

    The install time is the newer of the binary's mtime and the source-hash
    stamp's: a build that confirmed the installed binary current re-records the
    stamp without promoting a new inode, and that confirmation is as good as a
    promotion. A missing or unreadable stamp leaves the binary's mtime alone.

    None means the question could not be asked -- an unknown binary, an install
    from a release with no `crates/` to compare against, a binary that is not
    installed, or an unreadable git history. A caller reporting this should say
    nothing rather than guess: a false "stale" on a release install is noise.
    """
    crates = WORKSPACE_BINARY_CRATES.get(name)
    if crates is None:
        return None
    root = _workspace_crates_root()
    if root is None:
        return None
    bin_root = bin_dir or native_bin_dir()
    try:
        installed_at = (bin_root / name).stat().st_mtime
    except OSError:
        return None
    try:
        confirmed_at = source_hash_path(bin_root, name).stat().st_mtime
    except OSError:
        confirmed_at = installed_at
    committed_at = last_source_commit_time(root, crates)
    if committed_at is None:
        return None
    return committed_at > max(installed_at, confirmed_at)
