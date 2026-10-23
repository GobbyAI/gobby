"""Release window: one expiring lease that holds landings and new-team admission.

The lease is a JSON file beside the landing freeze, at
``<git-common-dir>/gobby/release-window.json``. Acquisition, renewal and release run
under ``MainCheckoutLanding(project_id)``, the lock ``land_candidate`` holds, so they
serialize against landing. Expiry is judged against an explicit ``now``. Nothing renews
the lease in the background: the owner renews at least every ``RENEW_INTERVAL``, and
every renewal bumps ``generation``.

An unreadable file is never repaired here. Reads return ``ReleaseWindowUnreadable``,
admissions stay closed, and mutations refuse with ``release_window_unreadable``.
Operators recover by deleting the file.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.storage.hub.protocol import MainCheckoutLanding
from gobby.storage.project_checkouts import require_root
from gobby.sync.jsonl_io import atomic_write_text
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

WINDOW_FILE_NAME = "release-window.json"
LEASE_TTL = timedelta(minutes=10)
RENEW_INTERVAL = timedelta(seconds=60)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_TOKEN_BYTES = 16
_UNREADABLE_MESSAGE = "release window state is unreadable; delete release-window.json to recover"


@dataclass(frozen=True)
class ReleaseWindow:
    """A live release lease, pinned to the integration tip read when it was acquired.

    ``generation`` increases on every acquire and renew. ``token`` is random per
    acquisition and identifies the owner's lease to the fencing checks.
    """

    owner_session_id: str
    token: str
    candidate_sha: str
    branch: str
    started_at: datetime
    expires_at: datetime
    generation: int

    def active(self, now: datetime) -> bool:
        """Return True while ``now`` is strictly before ``expires_at``."""
        return now < self.expires_at


@dataclass(frozen=True)
class ReleaseWindowUnreadable:
    """Malformed or unreadable coordination state. Admissions stay closed until it is cleared."""


class ReleaseWindowRefused(Exception):
    """A window operation was refused. ``code`` names the refusal for landing and callers.

    Codes: ``release_window_active``, ``release_window_unreadable``,
    ``release_window_stale``, ``release_window_missing``, and ``main_checkout_unavailable``
    when the main checkout cannot be resolved to a branch and tip.
    """

    code: str

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def window_path(git_common_dir: str | Path) -> Path:
    """Return the release-window file for the repository that owns ``git_common_dir``."""
    return Path(git_common_dir) / "gobby" / WINDOW_FILE_NAME


def read_window(git_common_dir: str | Path) -> ReleaseWindow | ReleaseWindowUnreadable | None:
    """Read the lease. A missing file is None; malformed or unreadable state is unreadable.

    Datetimes are stored as ISO-8601 UTC strings. An expired lease is returned as stored;
    ``admissions_closed`` is what applies expiry.
    """
    try:
        raw = window_path(git_common_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ReleaseWindowUnreadable()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ReleaseWindowUnreadable()
    return _parse(data)


def admissions_closed(state: ReleaseWindow | ReleaseWindowUnreadable | None, now: datetime) -> bool:
    """Return True for a live lease or unreadable state; False for no lease or an expired one."""
    if state is None:
        return False
    if isinstance(state, ReleaseWindowUnreadable):
        return True
    return state.active(now)


async def require_admissions_open(db: HubDatabase, project_id: str, now: datetime) -> None:
    """Refuse new work while a live or unreadable window holds admissions.

    New-team admission calls this under ``MainCheckoutLanding(project_id)``, so it
    serializes with acquisition. Work inside an existing team never calls it.
    """
    await _require_open(await _project_common_dir(db, project_id), now)


async def require_tree_admissions_open(checkout: Path, now: datetime) -> None:
    """Refuse a fresh worktree or clone of ``checkout`` while a window holds new work.

    Every checkout of a repository shares its Git common directory, which holds the window.
    The read takes no lock: tree creation never moves the integration tip, so a lost race
    costs one extra tree and no candidate drift.
    """
    await _require_open(await _git_common_dir(checkout), now)


async def _require_open(common: Path, now: datetime) -> None:
    state = await asyncio.to_thread(read_window, common)
    if isinstance(state, ReleaseWindowUnreadable):
        raise ReleaseWindowRefused("release_window_unreadable", _UNREADABLE_MESSAGE)
    if admissions_closed(state, now):
        raise ReleaseWindowRefused(
            "release_window_active",
            "a release window holds new teams and fresh trees; retry after it ends",
        )


async def acquire_window(
    db: HubDatabase, *, project_id: str, owner_session_id: str, now: datetime
) -> ReleaseWindow:
    """Take the release window for the project's main checkout.

    Runs under ``MainCheckoutLanding(project_id)``. Refuses ``release_window_active`` while
    a live lease exists and ``release_window_unreadable`` for malformed state. Otherwise it
    writes generation ``previous + 1`` (1 when none), a fresh token, ``expires_at`` of
    ``now + LEASE_TTL`` and ``candidate_sha`` set to the branch tip read inside the lock.
    """
    # Imported here because land_commit imports this module to check admissions under its lock.
    from gobby.tasks.land_commit import LandingGitError, main_checkout_target

    async with db.advisory_lock(MainCheckoutLanding(project_id)):
        try:
            target = await main_checkout_target(db, project_id)
        except LandingGitError as exc:
            raise ReleaseWindowRefused("main_checkout_unavailable", str(exc)) from exc
        if target is None:
            raise ReleaseWindowRefused("main_checkout_unavailable", "main checkout is detached")
        main, branch = target
        common = await _git_common_dir(main)
        state = await asyncio.to_thread(read_window, common)
        if isinstance(state, ReleaseWindowUnreadable):
            raise ReleaseWindowRefused("release_window_unreadable", _UNREADABLE_MESSAGE)
        if state is not None and state.active(now):
            raise ReleaseWindowRefused("release_window_active", "a release window is already held")
        window = ReleaseWindow(
            owner_session_id=owner_session_id,
            token=secrets.token_hex(_TOKEN_BYTES),
            candidate_sha=await _branch_tip(main, branch),
            branch=branch,
            started_at=now,
            expires_at=now + LEASE_TTL,
            generation=1 if state is None else state.generation + 1,
        )
        await asyncio.to_thread(_store, common, window)
        return window


async def renew_window(
    db: HubDatabase,
    *,
    project_id: str,
    owner_session_id: str,
    token: str,
    generation: int,
    now: datetime,
) -> ReleaseWindow:
    """Extend the caller's live lease. The stored owner, token and generation must match.

    Refuses ``release_window_stale`` on any mismatch or expiry, and
    ``release_window_missing`` when no lease is stored. Writes generation ``+ 1`` and
    ``expires_at`` of ``now + LEASE_TTL``. ``candidate_sha`` is unchanged.
    """
    async with db.advisory_lock(MainCheckoutLanding(project_id)):
        common = await _project_common_dir(db, project_id)
        current = _fenced(
            await asyncio.to_thread(read_window, common),
            owner_session_id=owner_session_id,
            token=token,
            generation=generation,
            now=now,
        )
        renewed = replace(current, expires_at=now + LEASE_TTL, generation=current.generation + 1)
        await asyncio.to_thread(_store, common, renewed)
        return renewed


async def release_window(
    db: HubDatabase,
    *,
    project_id: str,
    owner_session_id: str,
    token: str,
    generation: int,
    now: datetime,
) -> None:
    """Remove the caller's lease under the same lock and fencing checks as renewal.

    A stale owner never clears a successor's window.
    """
    async with db.advisory_lock(MainCheckoutLanding(project_id)):
        common = await _project_common_dir(db, project_id)
        _fenced(
            await asyncio.to_thread(read_window, common),
            owner_session_id=owner_session_id,
            token=token,
            generation=generation,
            now=now,
        )
        await asyncio.to_thread(_clear, common)


def require_window_owner(
    git_common_dir: str | Path,
    *,
    owner_session_id: str,
    token: str,
    generation: int,
    now: datetime,
) -> ReleaseWindow:
    """Fence an irreversible step by returning the live lease the caller still owns.

    Raises ``release_window_stale`` on any owner, token or generation mismatch, or when
    the lease has expired at ``now``.
    """
    return _fenced(
        read_window(git_common_dir),
        owner_session_id=owner_session_id,
        token=token,
        generation=generation,
        now=now,
    )


def _parse(data: object) -> ReleaseWindow | ReleaseWindowUnreadable:
    if not isinstance(data, dict):
        return ReleaseWindowUnreadable()
    owner = data.get("owner_session_id")
    token = data.get("token")
    candidate_sha = data.get("candidate_sha")
    branch = data.get("branch")
    generation = data.get("generation")
    started_raw = data.get("started_at")
    expires_raw = data.get("expires_at")
    if (
        not isinstance(owner, str)
        or not isinstance(token, str)
        or not isinstance(candidate_sha, str)
        or not _FULL_SHA.fullmatch(candidate_sha)
        or not isinstance(branch, str)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(started_raw, str)
        or not isinstance(expires_raw, str)
    ):
        return ReleaseWindowUnreadable()
    started_at = _parse_instant(started_raw)
    expires_at = _parse_instant(expires_raw)
    if started_at is None or expires_at is None:
        return ReleaseWindowUnreadable()
    return ReleaseWindow(
        owner_session_id=owner,
        token=token,
        candidate_sha=candidate_sha,
        branch=branch,
        started_at=started_at,
        expires_at=expires_at,
        generation=generation,
    )


def _parse_instant(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _serialize(window: ReleaseWindow) -> str:
    payload = {
        "owner_session_id": window.owner_session_id,
        "token": window.token,
        "candidate_sha": window.candidate_sha,
        "branch": window.branch,
        "started_at": window.started_at.astimezone(UTC).isoformat(),
        "expires_at": window.expires_at.astimezone(UTC).isoformat(),
        "generation": window.generation,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _store(git_common_dir: Path, window: ReleaseWindow) -> None:
    atomic_write_text(window_path(git_common_dir), _serialize(window))


def _clear(git_common_dir: Path) -> None:
    window_path(git_common_dir).unlink(missing_ok=True)


def _fenced(
    state: ReleaseWindow | ReleaseWindowUnreadable | None,
    *,
    owner_session_id: str,
    token: str,
    generation: int,
    now: datetime,
) -> ReleaseWindow:
    """Return ``state`` when the caller holds it live; otherwise raise the matching refusal."""
    if state is None:
        raise ReleaseWindowRefused("release_window_missing", "no release window is held")
    if isinstance(state, ReleaseWindowUnreadable):
        raise ReleaseWindowRefused("release_window_unreadable", _UNREADABLE_MESSAGE)
    if (
        state.owner_session_id != owner_session_id
        or state.token != token
        or state.generation != generation
    ):
        raise ReleaseWindowRefused(
            "release_window_stale", "the release window belongs to another lease"
        )
    if not state.active(now):
        raise ReleaseWindowRefused("release_window_stale", "the release window has expired")
    return state


async def _git_text(cwd: Path, *args: str) -> str:
    result = await daemon_git.run(args, cwd=cwd)
    if not isinstance(result, GitOk):
        raise ReleaseWindowRefused(
            "main_checkout_unavailable", f"git {' '.join(args)} failed: {result.stderr}"
        )
    return result.stdout.strip()


async def _git_common_dir(cwd: Path) -> Path:
    return Path(await _git_text(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))


async def _project_common_dir(db: HubDatabase, project_id: str) -> Path:
    # Same resolution as land_candidate, which also works while the branch is detached.
    repo = await asyncio.to_thread(require_root, db, project_id, require_machine_id())
    return await _git_common_dir(Path(repo))


async def _branch_tip(main: Path, branch: str) -> str:
    """Return the full 40-hex SHA that ``branch`` points at in the main checkout."""
    tip = await _git_text(main, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    if not _FULL_SHA.fullmatch(tip):
        raise ReleaseWindowRefused("main_checkout_unavailable", f"branch {branch} has no commit")
    return tip
