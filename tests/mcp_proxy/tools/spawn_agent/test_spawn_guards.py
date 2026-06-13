from __future__ import annotations

import threading

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _spawn_guards


@pytest.mark.asyncio
async def test_reserve_agent_slot_counts_active_agents_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calling_thread = threading.current_thread()
    count_threads: list[threading.Thread] = []

    def count_active_agents(_db: object, _project_id: str) -> int:
        count_threads.append(threading.current_thread())
        return 0

    monkeypatch.setattr(_spawn_guards, "_count_active_agents", count_active_agents)
    monkeypatch.setattr(_spawn_guards, "max_active_agents_for_project", lambda _path: 1)

    async with _spawn_guards.reserve_agent_slot(
        db=object(),
        project_id="project-off-thread-count",
        project_path="/tmp/project-off-thread-count",
    ) as response:
        assert response is None

    assert count_threads
    assert count_threads[0] is not calling_thread


def test_task_spawn_lease_releases_mutex_when_enter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class ExplodingMutex:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("init")

        def __enter__(self) -> object:
            events.append("enter")
            raise RuntimeError("post-acquire failure")

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            _traceback: object,
        ) -> bool:
            events.append(("exit", exc_type, str(exc)))
            return False

    monkeypatch.setattr("gobby.dispatch.mutex.RuntimeDispatchMutex", ExplodingMutex)
    monkeypatch.setattr(_spawn_guards, "TaskDispatchMutexManager", lambda _db: object())

    lease = _spawn_guards.TaskSpawnLease(db=object(), task_id="task-1")

    with pytest.raises(RuntimeError, match="post-acquire failure"):
        lease.acquire()

    assert events == ["init", "enter", ("exit", RuntimeError, "post-acquire failure")]
    assert lease._mutex is None
    assert lease._owns_mutex is False
