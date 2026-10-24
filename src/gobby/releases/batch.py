"""Release batch: validate, activate and smoke the release window's pinned candidate.

One batch runs as a long-lived CLI process because it outlives the daemon it
restarts. A keeper renews the window through the hub database while every
synchronous step runs in a worker thread. Each batch writes one JSON record under
``<git-common-dir>/gobby/release-batches/``; the newest succeeded record's
candidate is the release baseline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess  # nosec B404 # argv-form git through the spawn helper
import tomllib
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from gobby.install.bin_release_identity import crate_closure
from gobby.releases.smoke import SmokeResult
from gobby.releases.window import (
    RENEW_INTERVAL,
    ReleaseWindowRefused,
    release_window,
    renew_window,
    require_window_owner,
    sync_git_common_dir,
)
from gobby.storage.project_checkouts import require_root
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.jsonl_io import atomic_write_text
from gobby.utils import spawn
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

ActivationKind = Literal["cutover", "restart"]
Phase = Literal["checks", "activation", "smokes", "lease"]

_PYTHON_CHECKS = ("lint", "format", "type_check")
_WEB_CHECKS = ("ts_check", "frontend_lint", "frontend_tests")
_CARGO_ROOT_FILES = frozenset({"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"})
# The isolated test hub prefix the require-pytest-guard-env rule demands of agents.
# Without it hub-backed tests skip and the check reads as passing.
_PYTEST_GUARD = (
    "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
    "GOBBY_TEST_PROTECT=1"
)
_TAIL = 2000


@dataclass(frozen=True)
class Check:
    name: str
    command: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: str
    success: bool
    output: str


class ActivationFailed(RuntimeError):
    """The cutover or restart did not complete."""


class ReleaseBatchRefused(Exception):
    """The batch refused before its first renewal and left the window untouched.

    Codes: ``release_baseline_missing``, ``release_base_not_ancestor`` and
    ``release_candidate_unpinned``.
    """

    code: str

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class _BatchFailed(Exception):
    def __init__(self, phase: Phase, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.phase = phase


def _wall_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BatchEnvironment:
    """Synchronous ports. The batch runs each one in a worker thread."""

    command: Callable[[str], str | None]
    run_check: Callable[[Check], CheckResult]
    activation_kind: Callable[[], ActivationKind]
    activate: Callable[[ActivationKind], None]
    capture: Callable[[], frozenset[str]]
    smoke: Callable[[frozenset[str]], list[SmokeResult]]
    announce: Callable[[str], str | None]
    clock: Callable[[], datetime] = _wall_clock
    renew_every: float = RENEW_INTERVAL.total_seconds()


@dataclass(frozen=True)
class BatchRecord:
    base_sha: str
    candidate_sha: str
    token: str
    started_at: str
    check_plan: tuple[Check, ...] = ()
    results: tuple[CheckResult, ...] = ()
    activation: ActivationKind | None = None
    smokes: tuple[SmokeResult, ...] = ()
    status: Literal["running", "succeeded", "failed"] = "running"
    phase: Phase | None = None
    diagnostics: tuple[str, ...] = field(default=())
    regression_task: str | None = None


def records_dir(git_common_dir: str | Path) -> Path:
    return Path(git_common_dir) / "gobby" / "release-batches"


def latest_baseline(git_common_dir: str | Path) -> str | None:
    """Return the candidate of the newest succeeded record, or None."""
    directory = records_dir(git_common_dir)
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Skipping unreadable release batch record %s", path)
            continue
        if isinstance(data, dict) and data.get("status") == "succeeded":
            candidate = data.get("candidate_sha")
            if isinstance(candidate, str):
                return candidate
    return None


def check_plan(
    root: Path, changed: Iterable[str], command: Callable[[str], str | None]
) -> tuple[Check, ...]:
    """Choose the checks for the changed paths; unconfigured commands are omitted."""
    paths = [PurePosixPath(path) for path in changed]
    plan: list[Check] = []

    def configured(*names: str) -> None:
        plan.extend(Check(name, found) for name in names if (found := command(name)))

    if any(path.suffix == ".py" for path in paths):
        configured(*_PYTHON_CHECKS)
    packages = sorted(
        {pkg for path in paths if (pkg := _test_package(path)) and (root / "tests" / pkg).is_dir()}
    )
    if packages:
        targets = " ".join(f"tests/{pkg}" for pkg in packages)
        plan.append(Check("pytest", f"{_PYTEST_GUARD} uv run pytest {targets} -q"))
    if any(_is_cargo_input(path) for path in paths):
        configured("cargo_fmt")
        if crates := _affected_crates(root, paths):
            flags = " ".join(f"-p {name}" for name in crates)
            plan.append(Check("clippy", f"cargo clippy {flags} -- -D warnings"))
            plan.append(Check("nextest", f"cargo nextest run {flags}"))
    if any(path.parts[:1] == ("web",) for path in paths):
        configured(*_WEB_CHECKS)
    return tuple(plan)


def _test_package(path: PurePosixPath) -> str | None:
    if path.parts[:2] == ("src", "gobby") and len(path.parts) > 3:
        return path.parts[2]
    if path.parts[:1] == ("tests",) and len(path.parts) > 2:
        return path.parts[1]
    return None


def _is_cargo_input(path: PurePosixPath) -> bool:
    return path.parts[:1] in (("crates",), (".cargo",)) or str(path) in _CARGO_ROOT_FILES


def _affected_crates(root: Path, paths: list[PurePosixPath]) -> list[str]:
    """Package names of changed members and every member that path-depends on one."""
    base = root.resolve()
    workspace = tomllib.loads((base / "Cargo.toml").read_text(encoding="utf-8"))
    patterns = workspace.get("workspace", {}).get("members", [])
    members = {
        member.resolve()
        for pattern in patterns
        for member in base.glob(pattern)
        if (member / "Cargo.toml").is_file()
    }
    if any(str(path) in _CARGO_ROOT_FILES or path.parts[:1] == (".cargo",) for path in paths):
        selected = members
    else:
        changed = {
            member
            for member in members
            for path in paths
            if path.is_relative_to(member.relative_to(base).as_posix())
        }
        selected = {member for member in members if crate_closure(base, member) & changed}
    return sorted(
        tomllib.loads((member / "Cargo.toml").read_text(encoding="utf-8"))["package"]["name"]
        for member in selected
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return spawn.run(["git", *args], cwd=root, capture_output=True, text=True)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@dataclass(frozen=True)
class _Prepared:
    root: Path
    common: Path
    base: str
    candidate: str
    plan: tuple[Check, ...]


def _prepare(
    db: HubDatabase,
    project_id: str,
    owner_session_id: str,
    token: str,
    generation: int,
    base_sha: str | None,
    env: BatchEnvironment,
) -> _Prepared:
    """Every refusal that must leave the window untouched, then the check plan."""
    root = Path(require_root(db, project_id, require_machine_id()))
    common = sync_git_common_dir(root)
    window = require_window_owner(
        common,
        owner_session_id=owner_session_id,
        token=token,
        generation=generation,
        now=env.clock(),
    )
    candidate = window.candidate_sha
    if _head(root) != candidate:
        raise ReleaseBatchRefused(
            "release_candidate_unpinned",
            f"the main checkout HEAD is not the pinned candidate {candidate[:12]}",
        )
    base = latest_baseline(common) or base_sha
    if base is None:
        raise ReleaseBatchRefused(
            "release_baseline_missing", "no release batch has succeeded; pass --base-sha"
        )
    resolved = (
        _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        if not base.startswith("-")
        else None
    )
    if (
        resolved is None
        or resolved.returncode != 0
        or _git(root, "merge-base", "--is-ancestor", resolved.stdout.strip(), candidate).returncode
    ):
        raise ReleaseBatchRefused(
            "release_base_not_ancestor",
            f"{base} is not an ancestor of the pinned candidate {candidate[:12]}",
        )
    base = resolved.stdout.strip()
    diff = _git(root, "diff", "--name-only", base, candidate)
    if diff.returncode != 0:
        raise ReleaseBatchRefused("release_base_not_ancestor", diff.stderr.strip())
    plan = check_plan(root, diff.stdout.splitlines(), env.command)
    return _Prepared(root, common, base, candidate, plan)


@dataclass
class _Keeper:
    """Renews the lease every ``renew_every`` seconds and records loss instead of raising.

    Only a refusal loses the lease; any other renewal error retries on the next interval.
    """

    db: HubDatabase
    project_id: str
    owner_session_id: str
    token: str
    generation: int
    env: BatchEnvironment
    lost: str | None = None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=self.env.renew_every)
                return
            except TimeoutError:
                pass
            try:
                renewed = await renew_window(
                    self.db,
                    project_id=self.project_id,
                    owner_session_id=self.owner_session_id,
                    token=self.token,
                    generation=self.generation,
                    now=self.env.clock(),
                )
            except ReleaseWindowRefused as refused:
                self.lost = f"{refused.code}: {refused}"
                return
            except Exception:  # A crash here would cancel the batch mid-activation.
                # Transient: retry next interval. The lease outlives several intervals,
                # and a renewal that never lands is refused once the lease expires.
                logger.exception("Release window renewal failed; retrying next interval")
                continue
            self.generation = renewed.generation


@dataclass
class _Batch:
    db: HubDatabase
    project_id: str
    owner_session_id: str
    token: str
    env: BatchEnvironment
    prepared: _Prepared
    keeper: _Keeper
    record: BatchRecord
    path: Path
    phase: Phase = "checks"
    before_sent: bool = False

    async def run(self) -> tuple[Phase, str] | None:
        """Run every step; return the failed phase and diagnostic, or None."""
        try:
            await self._checks()
            self.phase = "activation"
            captured = await self._activate()
            self.phase = "smokes"
            await self._smokes(captured)
            # A lease lost during the smokes leaves the result unowned: no baseline moves.
            await asyncio.to_thread(self._prove)
        except _BatchFailed as failed:
            return failed.phase, str(failed)
        except Exception as exc:  # Job boundary: record the failure, release and file it.
            logger.exception("Release batch step failed")
            return self.phase, f"{type(exc).__name__}: {exc}"
        return None

    def _prove(self) -> None:
        if self.keeper.lost is not None:
            raise _BatchFailed("lease", self.keeper.lost)
        try:
            require_window_owner(
                self.prepared.common,
                owner_session_id=self.owner_session_id,
                token=self.token,
                now=self.env.clock(),
            )
        except ReleaseWindowRefused as refused:
            raise _BatchFailed("lease", f"{refused.code}: {refused}") from refused

    async def _checks(self) -> None:
        for check in self.record.check_plan:
            result = await asyncio.to_thread(self.env.run_check, check)
            self.record = replace(self.record, results=(*self.record.results, result))
            await asyncio.to_thread(_write_record, self.path, self.record)
            if not result.success:
                raise _BatchFailed("checks", f"{check.name} failed: {result.output[-_TAIL:]}")
            await asyncio.to_thread(self._prove)

    async def _activate(self) -> frozenset[str]:
        kind = await asyncio.to_thread(self.env.activation_kind)
        if await asyncio.to_thread(_head, self.prepared.root) != self.prepared.candidate:
            raise _BatchFailed("activation", "the main checkout HEAD moved off the candidate")
        captured = await asyncio.to_thread(self.env.capture)
        await asyncio.to_thread(self._prove)
        self.record = replace(self.record, activation=kind)
        await self.announce(
            f"BEFORE: release batch {self.prepared.candidate[:12]} starts a daemon {kind} now. "
            "DAEMON BACK follows when it is healthy."
        )
        self.before_sent = True
        try:
            await asyncio.to_thread(self.env.activate, kind)
        except ActivationFailed as exc:
            raise _BatchFailed("activation", str(exc)[-_TAIL:]) from exc
        return captured

    async def _smokes(self, captured: frozenset[str]) -> None:
        results = await asyncio.to_thread(self.env.smoke, captured)
        self.record = replace(self.record, smokes=tuple(results))
        if failed := [result for result in results if not result.ok]:
            raise _BatchFailed(
                "smokes", "; ".join(f"{result.name}: {result.detail}" for result in failed)
            )

    async def announce(self, text: str) -> None:
        if error := await asyncio.to_thread(self.env.announce, text):
            self.note(error)

    def note(self, diagnostic: str) -> None:
        self.record = replace(self.record, diagnostics=(*self.record.diagnostics, diagnostic))

    async def release(self) -> None:
        """Release the window if this batch still owns it; a successor's window survives."""
        try:
            await release_window(
                self.db,
                project_id=self.project_id,
                owner_session_id=self.owner_session_id,
                token=self.token,
                generation=self.keeper.generation,
                now=self.env.clock(),
            )
        except ReleaseWindowRefused as refused:
            self.note(f"window not released: {refused.code}: {refused}")

    async def cancelled(self) -> None:
        """Close a batch cancelled mid-run; the candidate did not fail, so no task is filed."""
        self.note(f"release batch cancelled in {self.phase}")
        self.record = replace(self.record, status="failed", phase=self.phase)
        await self.release()
        if self.before_sent:
            await self.announce(
                f"RELEASE FAILED: release batch {self.prepared.candidate[:12]} was cancelled "
                f"in {self.phase}"
            )
        await asyncio.to_thread(_write_record, self.path, self.record)


async def _to_completion(cleanup: Coroutine[object, object, None]) -> None:
    """Run ``cleanup`` to its end even when this task is cancelled again meanwhile."""
    task = asyncio.ensure_future(cleanup)
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    task.result()


async def run_batch(
    db: HubDatabase,
    *,
    project_id: str,
    owner_session_id: str,
    token: str,
    generation: int,
    base_sha: str | None,
    env: BatchEnvironment,
) -> BatchRecord:
    """Run one batch for the caller's live window and return its final record.

    Raises ``ReleaseBatchRefused`` or ``ReleaseWindowRefused`` before the first renewal,
    leaving the window as it was. Every later failure is recorded, releases the window
    and files one open regression task per candidate; a filing error is recorded as a
    diagnostic. A cancelled batch releases the window and records the failure, files no
    task, then re-raises.
    """
    prepared = await asyncio.to_thread(
        _prepare, db, project_id, owner_session_id, token, generation, base_sha, env
    )
    lease = await renew_window(
        db,
        project_id=project_id,
        owner_session_id=owner_session_id,
        token=token,
        generation=generation,
        now=env.clock(),
    )
    started = env.clock()
    record = BatchRecord(
        base_sha=prepared.base,
        candidate_sha=prepared.candidate,
        token=token,
        started_at=started.isoformat(),
        check_plan=prepared.plan,
    )
    path = records_dir(prepared.common) / (
        f"{started.astimezone(UTC):%Y%m%dT%H%M%S%fZ}-{prepared.candidate[:12]}.json"
    )
    await asyncio.to_thread(_write_record, path, record)
    keeper = _Keeper(db, project_id, owner_session_id, token, lease.generation, env)
    batch = _Batch(db, project_id, owner_session_id, token, env, prepared, keeper, record, path)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(keeper.run())
            try:
                failure = await batch.run()
            finally:
                keeper.stopped.set()
    except asyncio.CancelledError:
        await _to_completion(batch.cancelled())
        raise
    sha = prepared.candidate[:12]
    if failure is None:
        batch.record = replace(batch.record, status="succeeded")
        await asyncio.to_thread(_write_record, path, batch.record)
        await batch.release()
        await batch.announce(
            f"DAEMON BACK: release batch {sha} finished its {batch.record.activation}; "
            "smokes passed and it is the release baseline."
        )
    else:
        phase, diagnostic = failure
        batch.note(diagnostic)
        await batch.release()
        try:
            task_ref: str | None = await asyncio.to_thread(
                _regression_task, db, project_id, owner_session_id, batch.record, phase, diagnostic
            )
        except Exception as exc:  # The failure record must survive a hub outage.
            logger.exception("Release regression task filing failed")
            batch.note(f"regression task not filed: {type(exc).__name__}: {exc}")
            task_ref = None
        batch.record = replace(batch.record, status="failed", phase=phase, regression_task=task_ref)
        if batch.before_sent:
            await batch.announce(
                f"RELEASE FAILED: release batch {sha} failed in {phase}: {diagnostic[:300]}"
            )
    await asyncio.to_thread(_write_record, path, batch.record)
    return batch.record


def _regression_task(
    db: HubDatabase,
    project_id: str,
    owner_session_id: str,
    record: BatchRecord,
    phase: Phase,
    diagnostic: str,
) -> str:
    """Find or create the candidate's one open regression task.

    Written through the task manager directly because the daemon may be down.
    """
    label = f"release-regression-{record.candidate_sha[:12]}"
    tasks = LocalTaskManager(db)
    if existing := tasks.list_tasks(project_id=project_id, label=label, closed=False, limit=1):
        return existing[0].id
    description = (
        f"Release batch for candidate {record.candidate_sha} (base {record.base_sha}) "
        f"failed in phase {phase}.\n\n{diagnostic[-_TAIL:]}"
    )
    task = tasks.create_task(
        project_id,
        f"Release regression in {record.candidate_sha[:12]}",
        description,
        created_in_session_id=owner_session_id,
        priority=1,
        task_type="bug",
        labels=[label],
        category="code",
        validation_criteria=(
            f"The {phase} failure recorded for {record.candidate_sha[:12]} is fixed, and a "
            "release batch for a candidate containing the fix records status succeeded."
        ),
    )
    return task.id


def _write_record(path: Path, record: BatchRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(asdict(record), indent=2) + "\n")
