"""Release window: expiry, fencing and atomic admission against landing."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.release_tools import register_release_tools
from gobby.releases.window import (
    LEASE_TTL,
    ReleaseWindowRefused,
    ReleaseWindowUnreadable,
    acquire_window,
    admissions_closed,
    read_window,
    release_window,
    renew_window,
    require_window_owner,
    window_path,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import get_current_session_id, session_context_for_test
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.review_approvals import grant_step_writes

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FULL_SHA = re.compile(r"[0-9a-f]{40}")


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


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
async def test_atomic_window_admission(case: LandingCase, ordering: str) -> None:
    common = case.repo / ".git"
    owner = case.session("release owner")
    sha = case.candidate("lane", {"docs/change.md": "land me\n"})
    task = case.reviewed(sha)
    pre_tip = case.git("rev-parse", "trunk")
    # land_candidate reads the wall clock, so the lease must be live at real time.
    live_now = datetime.now(UTC)

    if ordering == "race":
        landing, lease = await asyncio.gather(
            case.land(task, sha),
            acquire_window(
                case.db, project_id=case.project_id, owner_session_id=owner, now=live_now
            ),
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
    else:
        lease = await acquire_window(
            case.db, project_id=case.project_id, owner_session_id=owner, now=live_now
        )
        held = await case.land(task, sha)
        assert held.get("landed") is False
        assert "release_window_active" in held.get("blockers", [])
        assert case.git("rev-parse", "trunk") == lease.candidate_sha == pre_tip

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
