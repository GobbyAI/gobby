"""Release window: expiry, fencing and atomic admission against landing."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.isolation_models import IsolationContext, SpawnConfig
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.agents.team_lifecycle import TeamAdmissionRefused, TeamBinding
from gobby.config.app import DaemonConfig
from gobby.hub import machine_routing, remote_spawn
from gobby.hub.machine_routing import RoutedMachine
from gobby.hub.remote_spawn import RemoteSpawnRequest
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.release_tools import register_release_tools
from gobby.mcp_proxy.tools.spawn_agent._isolation_prepare import prepare_spawn_isolation
from gobby.mcp_proxy.tools.spawn_agent._remote_routing import spawn_remote_route
from gobby.releases.window import (
    LEASE_TTL,
    ReleaseWindowRefused,
    ReleaseWindowUnreadable,
    acquire_window,
    admissions_closed,
    agent_activation_refusal,
    read_window,
    release_window,
    renew_window,
    require_window_owner,
    route_activation_refusal,
    window_path,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.local_token import GOBBY_RELEASE_TOKEN_ENV
from gobby.utils.session_context import get_current_session_id, session_context_for_test
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.postgres import TEST_USER_ID
from tests.fixtures.review_approvals import grant_step_writes
from tests.fixtures.team_case import TITLE, TeamWorld, execution_id, make_team_world

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FULL_SHA = re.compile(r"[0-9a-f]{40}")


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


@pytest.fixture
def world(case: LandingCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TeamWorld:
    return make_team_world(case, tmp_path, monkeypatch)


async def _admission(world: TeamWorld, caller: Session, task: Task) -> TeamBinding | str:
    """Admit ``caller``'s team for ``task``, or return the refusal code."""
    try:
        return await world.admit(caller, task)
    except TeamAdmissionRefused as exc:
        return exc.code


async def _fresh_tree(
    case: LandingCase, task: Task
) -> tuple[IsolationContext | None, dict[str, Any] | None]:
    """Prepare a fresh worktree for ``task`` through a direct spawn's isolation seam."""
    config = SpawnConfig(
        prompt="Work the task",
        task_id=task.id,
        task_title=task.title,
        task_seq_num=task.seq_num,
        branch_name=None,
        branch_prefix=None,
        base_branch="trunk",
        project_id=case.project_id,
        project_path=str(case.repo),
        provider="claude",
        parent_session_id=case.creator,
    )
    trees = LocalWorktreeManager(case.db)
    handler = WorktreeIsolationHandler(WorktreeGitManager(str(case.repo)), trees)
    return await prepare_spawn_isolation(handler, config)


async def test_expiry_and_fencing(case: LandingCase) -> None:
    common = case.repo / ".git"
    project_id = case.project_id
    owner = case.session("release owner")
    intruder = case.session("second owner")
    trunk_tip = case.git("rev-parse", "trunk")

    # Acquisition pins the integration tip and starts at generation 1.
    lease = await acquire_window(case.db, project_id=project_id, owner_session_id=owner, now=NOW)
    assert lease.generation == 1
    assert lease.owner_session_id == owner
    assert FULL_SHA.fullmatch(lease.candidate_sha)
    assert lease.candidate_sha == trunk_tip
    assert lease.expires_at == NOW + LEASE_TTL
    assert window_path(common).is_file()
    assert read_window(common) == lease
    assert admissions_closed(read_window(common), NOW + timedelta(minutes=1))
    assert not admissions_closed(read_window(common), lease.expires_at)

    # A live lease refuses every other acquisition.
    with pytest.raises(ReleaseWindowRefused) as active:
        await acquire_window(
            case.db,
            project_id=project_id,
            owner_session_id=intruder,
            now=NOW + timedelta(minutes=1),
        )
    assert active.value.code == "release_window_active"

    # The owner renews with the current token and generation.
    renewed = await renew_window(
        case.db,
        project_id=project_id,
        owner_session_id=owner,
        token=lease.token,
        generation=lease.generation,
        now=NOW + timedelta(minutes=5),
    )
    assert renewed.generation == lease.generation + 1
    assert renewed.expires_at == NOW + timedelta(minutes=5) + LEASE_TTL
    assert renewed.candidate_sha == lease.candidate_sha
    assert read_window(common) == renewed

    # Stale renewals fail: a superseded generation, a wrong token, another owner.
    stale_renewals = [
        (owner, renewed.token, lease.generation),
        (owner, "not-the-token", renewed.generation),
        (intruder, renewed.token, renewed.generation),
    ]
    for stale_owner, stale_token, stale_generation in stale_renewals:
        with pytest.raises(ReleaseWindowRefused) as stale:
            await renew_window(
                case.db,
                project_id=project_id,
                owner_session_id=stale_owner,
                token=stale_token,
                generation=stale_generation,
                now=NOW + timedelta(minutes=6),
            )
        assert stale.value.code == "release_window_stale"
    assert read_window(common) == renewed

    # Once the lease lapses, a successor takes over and the first owner is fenced out.
    expired_at = renewed.expires_at
    assert not admissions_closed(read_window(common), expired_at)
    successor = await acquire_window(
        case.db, project_id=project_id, owner_session_id=intruder, now=expired_at
    )
    assert successor.owner_session_id == intruder
    assert successor.generation == renewed.generation + 1
    assert successor.candidate_sha == trunk_tip
    assert read_window(common) == successor

    with pytest.raises(ReleaseWindowRefused) as fenced_renew:
        await renew_window(
            case.db,
            project_id=project_id,
            owner_session_id=owner,
            token=renewed.token,
            generation=renewed.generation,
            now=expired_at,
        )
    assert fenced_renew.value.code == "release_window_stale"
    assert read_window(common) == successor

    with pytest.raises(ReleaseWindowRefused) as fenced_release:
        await release_window(
            case.db,
            project_id=project_id,
            owner_session_id=owner,
            token=renewed.token,
            generation=renewed.generation,
            now=expired_at,
        )
    assert fenced_release.value.code == "release_window_stale"
    assert read_window(common) == successor

    with pytest.raises(ReleaseWindowRefused) as fenced_activation:
        require_window_owner(
            common,
            owner_session_id=owner,
            token=renewed.token,
            generation=renewed.generation,
            now=expired_at,
        )
    assert fenced_activation.value.code == "release_window_stale"
    assert read_window(common) == successor

    # The successor's fence holds while live and lapses at its own expiry.
    live_until = successor.expires_at - timedelta(seconds=1)
    assert (
        require_window_owner(
            common,
            owner_session_id=intruder,
            token=successor.token,
            generation=successor.generation,
            now=live_until,
        )
        == successor
    )
    with pytest.raises(ReleaseWindowRefused) as lapsed:
        require_window_owner(
            common,
            owner_session_id=intruder,
            token=successor.token,
            generation=successor.generation,
            now=successor.expires_at,
        )
    assert lapsed.value.code == "release_window_stale"

    # The live owner releases, which removes the file.
    await release_window(
        case.db,
        project_id=project_id,
        owner_session_id=intruder,
        token=successor.token,
        generation=successor.generation,
        now=live_until,
    )
    assert read_window(common) is None
    assert not admissions_closed(read_window(common), expired_at)

    with pytest.raises(ReleaseWindowRefused) as missing:
        await renew_window(
            case.db,
            project_id=project_id,
            owner_session_id=intruder,
            token=successor.token,
            generation=successor.generation,
            now=live_until,
        )
    assert missing.value.code == "release_window_missing"

    # Malformed state fails visibly: admissions stay closed and acquisition refuses.
    window_path(common).parent.mkdir(parents=True, exist_ok=True)
    window_path(common).write_text("{not json", encoding="utf-8")
    assert isinstance(read_window(common), ReleaseWindowUnreadable)
    assert admissions_closed(read_window(common), NOW)
    with pytest.raises(ReleaseWindowRefused) as unreadable:
        await acquire_window(case.db, project_id=project_id, owner_session_id=owner, now=NOW)
    assert unreadable.value.code == "release_window_unreadable"


@pytest.mark.parametrize("ordering", ["race", "deterministic"])
async def test_atomic_window_admission(world: TeamWorld, ordering: str) -> None:
    case = world.case
    common = case.repo / ".git"
    owner = case.session("release owner")
    sha = case.candidate("lane", {"docs/change.md": "land me\n"})
    task = case.reviewed(sha)
    team_task = case.task(TITLE)
    admitting = world.child(team_task)
    pre_tip = case.git("rev-parse", "trunk")
    # land_candidate and admit_team read the wall clock, so the lease must be live at real time.
    live_now = datetime.now(UTC)

    if ordering == "race":
        landing, lease, admission = await asyncio.gather(
            case.land(task, sha),
            acquire_window(
                case.db, project_id=case.project_id, owner_session_id=owner, now=live_now
            ),
            _admission(world, admitting, team_task),
        )
        post_tip = case.git("rev-parse", "trunk")
        if landing.get("landed"):
            # Landing took the lock first, so the window pins the tip it landed.
            assert post_tip == sha
            assert lease.candidate_sha == sha
        else:
            # The window took the lock first: the landing is refused and nothing moves.
            assert "release_window_active" in landing.get("blockers", [])
            assert lease.candidate_sha == pre_tip
            assert post_tip == pre_tip
        if isinstance(admission, TeamBinding):
            # Admission took the lock before the window, so its team stands.
            assert world.task_branches() == [admission.branch]
        else:
            # The window took the lock first: no team is recorded and no tree is made.
            assert admission == "release_window_active"
            assert world.admit_step_output(admitting) == {}
            assert world.task_branches() == []
    else:
        lease = await acquire_window(
            case.db, project_id=case.project_id, owner_session_id=owner, now=live_now
        )
        held = await case.land(task, sha)
        assert held.get("landed") is False
        assert "release_window_active" in held.get("blockers", [])
        assert case.git("rev-parse", "trunk") == lease.candidate_sha == pre_tip
        assert await _admission(world, admitting, team_task) == "release_window_active"
        assert world.admit_step_output(admitting) == {}
        assert world.task_branches() == []

        await release_window(
            case.db,
            project_id=case.project_id,
            owner_session_id=owner,
            token=lease.token,
            generation=lease.generation,
            now=live_now,
        )
        assert read_window(common) is None
        landed = await case.land(task, sha)
        assert landed.get("landed") is True
        assert case.git("rev-parse", "trunk") == sha
        admitted = await _admission(world, admitting, team_task)
        assert isinstance(admitted, TeamBinding), admitted
        assert world.task_branches() == [admitted.branch]


async def test_existing_team_work_continues(world: TeamWorld) -> None:
    case = world.case
    common = case.repo / ".git"
    task = case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    # A team whose admit step recorded its intent and died before it made the tree.
    recovering_task = case.task("Recover the interrupted team")
    recovering = world.child(recovering_task)
    world.rewrite_admit_output(
        recovering,
        {
            "team": {
                "team_id": execution_id(recovering),
                "task_id": recovering_task.id,
                "project_id": case.project_id,
                "branch": f"task-{recovering_task.seq_num}-recover-the-interrupted-team",
                "base_branch": "trunk",
            }
        },
    )
    new_task = case.task("Start new work")
    newcomer = world.child(new_task)
    solo_task = case.task("Work alone")
    orchestrator = case.session("orchestrator")
    sha = case.candidate("lane", {"docs/change.md": "land me\n"})
    landing_task = case.reviewed(sha)
    pre_tip = case.git("rev-parse", "trunk")
    lease = await acquire_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=case.session("release owner"),
        now=datetime.now(UTC),
    )

    # The existing team keeps working: its admission re-runs, its seats join its tree,
    # and a replacement execution rejoins it.
    assert await world.admit(original, task) == team
    for target in ({"worktree_id": team.worktree_id}, {"project_path": team.worktree_path}):
        joined = await world.spawn(original, **target)
        assert joined["success"] is True, (target, joined)
    world.finish(original)
    replacement = world.child(task)
    assert await world.admit(replacement, task, team_id=team.team_id) == team
    rejoined = await world.spawn(replacement, worktree_id=team.worktree_id)
    assert rejoined["success"] is True, rejoined
    reused = await world.direct_spawn(
        orchestrator, task_id=task.id, worktree_id=team.worktree_id, checkout_mode="worktree"
    )
    assert reused["success"] is True, reused

    # A team admitted before the window recovers its tree.
    recovered = await world.admit(recovering, recovering_task)
    assert recovered.team_id == execution_id(recovering)
    assert Path(recovered.worktree_path).is_dir()

    # New teams, fresh direct trees and landings wait, under a live or an unreadable window.
    for code in ("release_window_active", "release_window_unreadable"):
        if code == "release_window_unreadable":
            window_path(common).write_text("{not json", encoding="utf-8")
        assert await _admission(world, newcomer, new_task) == code
        _held, refusal = await _fresh_tree(case, solo_task)
        assert (refusal or {}).get("error_code") == code, refusal
        assert world.admit_step_output(newcomer) == {}
        assert sorted(world.task_branches()) == sorted([team.branch, recovered.branch])
    landing = await case.land(landing_task, sha)
    assert "release_window_unreadable" in landing.get("blockers", [])
    assert case.git("rev-parse", "trunk") == lease.candidate_sha == pre_tip

    # Clearing the window reopens new work.
    window_path(common).unlink()
    admitted = await _admission(world, newcomer, new_task)
    assert isinstance(admitted, TeamBinding), admitted
    fresh, refusal = await _fresh_tree(case, solo_task)
    assert refusal is None and fresh is not None, refusal
    assert fresh.branch_name in world.task_branches()


async def test_release_tools_admit_only_the_release_manager(case: LandingCase) -> None:
    common = case.repo / ".git"
    manager = case.session("release manager")
    grant_step_writes(case.db, manager, case.project_id, "release-manager", ())
    outsider = case.session("developer")
    grant_step_writes(case.db, outsider, case.project_id, "developer", ())
    registry = InternalToolRegistry(name="test-agents", description="test")
    register_release_tools(
        registry,
        AgentsRegistryContext(
            runner=MagicMock(),
            agent_run_manager=LocalAgentRunManager(case.db),
            resolve_session_id=lambda ref: ref,
            get_current_session_id=get_current_session_id,
            get_current_agent_run_id=lambda: None,
            get_project_context=lambda: None,
            db=case.db,
        ),
    )

    with session_context_for_test(outsider):
        refused = await registry.call("acquire_release_window", {})
    assert refused["success"] is False
    assert refused["error_code"] == "release_window_forbidden"
    assert read_window(common) is None

    with session_context_for_test(manager):
        acquired = await registry.call("acquire_release_window", {})
    assert acquired["success"] is True
    lease = acquired["window"]
    assert lease["owner_session_id"] == manager
    assert lease["candidate_sha"] == case.git("rev-parse", "trunk")
    held = {"token": lease["token"], "generation": lease["generation"]}

    with session_context_for_test(outsider):
        stolen = await registry.call("end_release_window", held)
    assert stolen["error_code"] == "release_window_forbidden"

    with session_context_for_test(manager):
        renewed = await registry.call("renew_release_window", held)
        assert renewed["window"]["generation"] == lease["generation"] + 1
        stale = await registry.call("end_release_window", held)
        assert stale["success"] is False
        assert stale["error_code"] == "release_window_stale"
        ended = await registry.call(
            "end_release_window", {**held, "generation": renewed["window"]["generation"]}
        )
    assert ended == {"success": True}
    assert read_window(common) is None


async def test_only_the_release_manager_lease_activates_the_daemon(case: LandingCase) -> None:
    manager = case.session("release manager")
    grant_step_writes(case.db, manager, case.project_id, "release-manager", ())
    developer = case.session("developer")
    grant_step_writes(case.db, developer, case.project_id, "developer", ())
    now = datetime.now(UTC)
    # The lease is a plain file any shell can write; acquiring as the developer forges one.
    forged = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=developer, now=now
    )

    refusal = await route_activation_refusal(
        case.db, session_id=developer, project_id=case.project_id, token=forged.token, now=now
    )

    assert refusal is not None and refusal.startswith("release_window_forbidden"), refusal
    await release_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=developer,
        token=forged.token,
        generation=forged.generation,
        now=now,
    )
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=manager, now=now
    )
    assert (
        await route_activation_refusal(
            case.db, session_id=manager, project_id=case.project_id, token=lease.token, now=now
        )
        is None
    )


async def test_remote_fresh_trees_wait_for_the_window(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = case.repo / ".git"
    node_machine = str(uuid4())
    node_checkout = f"/node-only/{node_machine}/checkout"
    LocalMachineManager(case.db).upsert_seen(node_machine, TEST_USER_ID)
    case.db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (node_machine, case.project_id, node_checkout),
    )
    parent = str(uuid4())
    case.db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, status)
        VALUES (%s, %s, %s, 'claude', %s, 'active')
        """,
        (parent, f"remote-{parent}", node_machine, case.project_id),
    )
    node = AsyncMock()
    node.connected_machine_ids.return_value = {node_machine}
    node.send.return_value = {"accepted": True}
    monkeypatch.setattr(machine_routing, "node_command_client", lambda: node)
    monkeypatch.setattr(remote_spawn, "node_command_client", lambda: node, raising=False)
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "", 0)
    runner.run_storage = LocalAgentRunManager(case.db)

    async def remote_spawn_with(isolation: str | None) -> dict[str, Any]:
        return await spawn_remote_route(
            case.db,
            routed=RoutedMachine(node_machine, local=False, checkout_root=node_checkout),
            session_manager=SessionManager(case.db),
            task_manager=LocalTaskManager(case.db),
            runner=runner,
            daemon_config=DaemonConfig(),
            placement=None,
            request=RemoteSpawnRequest(
                parent_session_id=parent,
                project_id=case.project_id,
                prompt="Work on the node",
                provider="claude",
                isolation=isolation,
            ),
        )

    await acquire_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=case.session("release owner"),
        now=datetime.now(UTC),
    )

    # A fresh tree on another machine is new work, so this machine's window holds it.
    for code in ("release_window_active", "release_window_unreadable"):
        if code == "release_window_unreadable":
            window_path(common).write_text("{not json", encoding="utf-8")
        for isolation in ("worktree", "clone"):
            refused = await remote_spawn_with(isolation)
            assert refused.get("error_code") == code, (isolation, refused)
    node.send.assert_not_awaited()
    runs = case.db.fetchone("SELECT COUNT(*) AS n FROM agent_runs", ())
    assert runs is not None and int(runs["n"]) == 0

    # A run in the node's own checkout makes no tree and still launches.
    shared = await remote_spawn_with(None)
    assert shared["success"] is True, shared

    # Clearing the window reopens fresh trees.
    window_path(common).unlink()
    fresh = await remote_spawn_with("worktree")
    assert fresh["success"] is True, fresh
    assert node.send.await_count == 2


async def test_lease_expiring_past_one_ttl_is_not_live(case: LandingCase) -> None:
    """A hand-written lease cannot hold admissions and landings past one TTL."""
    common = case.repo / ".git"
    owner = case.session("release owner")
    successor = case.session("successor")
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=NOW
    )
    # The lease is a plain file any shell can write.
    path = window_path(common)
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["expires_at"] = (NOW + LEASE_TTL + timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(forged), encoding="utf-8")

    assert admissions_closed(read_window(common), NOW) is False
    with pytest.raises(ReleaseWindowRefused) as stale:
        require_window_owner(common, owner_session_id=owner, token=lease.token, now=NOW)
    assert stale.value.code == "release_window_stale"
    taken = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=successor, now=NOW
    )
    assert taken.generation == lease.generation + 1


async def test_cli_activation_needs_the_release_manager_lease(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI fence checks the lease owner's seat, as the activation route does."""
    manager = case.session("release manager")
    grant_step_writes(case.db, manager, case.project_id, "release-manager", ())
    developer = case.session("developer")
    grant_step_writes(case.db, developer, case.project_id, "developer", ())
    monkeypatch.chdir(case.repo)
    opened = case.lend_hub(monkeypatch)
    now = datetime.now(UTC)
    forged = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=developer, now=now
    )
    monkeypatch.setenv(GOBBY_RELEASE_TOKEN_ENV, forged.token)
    monkeypatch.setenv("GOBBY_SESSION_ID", developer)

    refusal = agent_activation_refusal()

    assert refusal is not None and refusal.startswith("release_window_forbidden"), refusal
    await release_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=developer,
        token=forged.token,
        generation=forged.generation,
        now=now,
    )
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=manager, now=now
    )
    monkeypatch.setenv(GOBBY_RELEASE_TOKEN_ENV, lease.token)
    monkeypatch.setenv("GOBBY_SESSION_ID", manager)
    assert agent_activation_refusal() is None
    # A fence only reads: it never migrates a hub the stopped daemon just released.
    assert opened == [{"apply_migrations": False}] * 2

    def unreadable_hub(**_options: object) -> HubDatabase:
        raise RuntimeError("PostgreSQL hub database is not configured")

    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", unreadable_hub)
    refusal = agent_activation_refusal()
    assert refusal is not None and refusal.startswith("release_window_forbidden"), refusal
