"""Red tests for dispatcher heartbeat execution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_candidate_filter_excludes_claimed_leased_blocked_terminal() -> None:
    from gobby.storage.tasks import _crud

    candidates = _crud.list_automation_candidates(project_id="project-1")

    assert all(not candidate.claimed_by_session_id for candidate in candidates)
    assert all(candidate.status != "closed" for candidate in candidates)
    assert all(not _crud.is_blocked_by_deps(candidate) for candidate in candidates)


async def test_max_active_agents_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    spawned: list[object] = []
    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *args, **kwargs: 2)
    monkeypatch.setattr(dispatcher, "MAX_ACTIVE_AGENTS", 2)
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert spawned == []


async def test_mutex_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    events: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "RuntimeDispatchMutex",
        lambda *args, **kwargs: SimpleNamespace(
            __enter__=lambda self: events.append("acquire") or self,
            __exit__=lambda self, *exc: events.append("release"),
        ),
    )

    await dispatcher.run_heartbeat(project_id="project-1")

    assert events == ["acquire", "release"]


async def test_toctou_skip_on_changed_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    executed: list[object] = []
    monkeypatch.setattr(dispatcher, "reload_candidate", lambda candidate: None)
    monkeypatch.setattr(dispatcher, "execute_action", lambda action: executed.append(action))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert executed == []


async def test_first_match_action_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    executed: list[object] = []
    monkeypatch.setattr(dispatcher, "execute_action", lambda action: executed.append(action))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert len(executed) == 1


async def test_spawn_action_links_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    attached: list[str] = []
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: "run-1")
    monkeypatch.setattr(dispatcher.RuntimeDispatchMutex, "attach", lambda self, run_id: attached.append(run_id))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert attached == ["run-1"]


async def test_advance_action_releases_lease_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    released: list[bool] = []
    monkeypatch.setattr(dispatcher.RuntimeDispatchMutex, "release", lambda self: released.append(True))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert released == [True]


async def test_start_expansion_action_links_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    attached: list[str] = []
    monkeypatch.setattr(dispatcher, "start_expansion_run_impl", lambda *args, **kwargs: "expansion-1")
    monkeypatch.setattr(dispatcher.RuntimeDispatchMutex, "attach", lambda self, run_id: attached.append(run_id))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert attached == ["expansion-1"]


async def test_expansion_terminal_event_releases_lease_via_handlers() -> None:
    from gobby.hooks.event_handlers import _dispatch

    assert hasattr(_dispatch, "on_expansion_run_completed")
    assert hasattr(_dispatch, "on_expansion_run_failed")
    assert hasattr(_dispatch, "on_expansion_run_cancelled")


async def test_attach_run_id_precedes_start_expansion_run_impl(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    events: list[str] = []
    monkeypatch.setattr(dispatcher.RuntimeDispatchMutex, "attach", lambda self, run_id: events.append("attach"))
    monkeypatch.setattr(
        dispatcher,
        "start_expansion_run_impl",
        lambda *args, **kwargs: events.append("start") or "expansion-1",
    )

    await dispatcher.run_heartbeat(project_id="project-1")

    assert events.index("attach") < events.index("start")


async def test_synchronous_terminal_expansion_releases_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    released: list[bool] = []
    monkeypatch.setattr(dispatcher.RuntimeDispatchMutex, "release", lambda self: released.append(True))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert released


def test_terminal_handler_release_by_task_id_fallback() -> None:
    from gobby.hooks.event_handlers import _dispatch

    assert hasattr(_dispatch.RuntimeDispatchMutex, "force_release_for_task")


async def test_dispatcher_pins_auto_apply_true_on_start_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    configs: list[dict] = []
    monkeypatch.setattr(dispatcher, "start_expansion_run_impl", lambda **kwargs: configs.append(kwargs) or "run-1")

    await dispatcher.run_heartbeat(project_id="project-1")

    assert configs[0]["auto_apply"] is True


async def test_create_isolation_action_writes_artifact_pair_and_base_commit_sha_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    writes: list[dict] = []
    monkeypatch.setattr(dispatcher, "set_artifacts_atomic", lambda **kwargs: writes.append(kwargs))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert writes
    assert {"worktree_path", "worktree_id", "base_commit_sha"} <= writes[0].keys()


async def test_create_isolation_action_resolves_base_commit_sha_from_target_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    resolved: list[str] = []
    monkeypatch.setattr(dispatcher, "resolve_branch_sha", lambda branch: resolved.append(branch) or "abc123")

    await dispatcher.run_heartbeat(project_id="project-1")

    assert resolved == ["main"]


async def test_create_isolation_action_missing_target_branch_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    escalations: list[object] = []
    monkeypatch.setattr(dispatcher, "escalate_task", lambda *args, **kwargs: escalations.append(kwargs))

    await dispatcher.run_heartbeat(project_id="project-1")

    assert escalations


async def test_dev_rule_fires_on_next_heartbeat_after_isolation_created() -> None:
    from gobby.dispatch import dispatcher

    first = await dispatcher.run_heartbeat(project_id="project-1")
    second = await dispatcher.run_heartbeat(project_id="project-1")

    assert first != second


async def test_startup_sweep_clears_expired_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    swept: list[bool] = []
    monkeypatch.setattr(dispatcher, "sweep_expired_leases", lambda *args, **kwargs: swept.append(True))

    await dispatcher.run_heartbeat(project_id="project-1", startup=True)

    assert swept == [True]
