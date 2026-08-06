from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from gobby.agents import terminal_cleanup
from gobby.agents.sandbox_policy import (
    SRT_SETTINGS_RELATIVE_PATH,
    SRT_VIOLATIONS_RELATIVE_PATH,
    managed_execution_root,
    prepare_sandbox_run_paths,
)
from gobby.agents.srt_process_cleanup import (
    ProcessIter,
    WaitProcs,
    _run_id_from_cmdline,
    reap_orphaned_srt_runner_process_trees,
    reap_srt_runner_process_tree,
)
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.runner_lifecycle_agents import _reap_orphaned_srt_runners_on_startup
from tests.agents.cleanup_test_support import RecordingDb, _handler, _run, _stub_runtime_cleanup

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit


class FakeProcess:
    def __init__(
        self,
        pid: int,
        cmdline: list[str],
        *,
        children: Iterable[FakeProcess] = (),
    ) -> None:
        self.pid = pid
        self.info: dict[str, object] = {"pid": pid, "cmdline": cmdline}
        self._children = list(children)
        self.terminated = False
        self.killed = False

    def children(self, *, recursive: bool) -> list[FakeProcess]:
        assert recursive is True
        descendants = list(self._children)
        for child in self._children:
            descendants.extend(child.children(recursive=True))
        return descendants

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _runner_process(
    run_id: str,
    pid: int,
    *,
    children: Iterable[FakeProcess] = (),
    managed_credentials: bool = False,
) -> FakeProcess:
    env = (
        {
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(
                managed_execution_root() / run_id / "bootstrap.json"
            )
        }
        if managed_credentials
        else {}
    )
    run_paths = prepare_sandbox_run_paths(run_id, env)
    launch = SandboxLaunch(
        backend="srt",
        enforced=True,
        provider_executable="/tmp/fake-provider",
        policy_path=str(run_paths.root / SRT_SETTINGS_RELATIVE_PATH),
        violation_path=str(run_paths.root / SRT_VIOLATIONS_RELATIVE_PATH),
        node_path="/tmp/fake-gobby/tools/srt/0.1.0/node",
        runner_path="/tmp/fake-gobby/tools/srt/0.1.0/runner.mjs",
    )
    return FakeProcess(
        pid,
        launch.wrap(["provider", "--version"]),
        children=children,
    )


def _process_iter(
    processes: list[FakeProcess],
) -> ProcessIter:
    def iterate(_attrs: list[str]) -> Iterable[FakeProcess]:
        return iter(processes)

    return cast(ProcessIter, iterate)


def _wait_procs(
    processes: list[FakeProcess],
    *,
    timeout: float,
) -> tuple[list[FakeProcess], list[FakeProcess]]:
    assert timeout > 0
    return processes, []


@pytest.mark.asyncio
@pytest.mark.parametrize("managed_credentials", [False, True], ids=["fallback", "managed"])
async def test_terminal_transition_reaps_surviving_srt_runner_tree(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    managed_credentials: bool,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    child = FakeProcess(102, ["codex", "app-server"])
    runner_process = _runner_process(
        "run-1",
        101,
        children=[child],
        managed_credentials=managed_credentials,
    )
    sandbox_root = gobby_home / "run" / "sandbox"
    assert _run_id_from_cmdline(runner_process.info["cmdline"], sandbox_root) == "run-1"
    wait_calls = 0

    def wait_for_exit(
        processes: list[FakeProcess],
        *,
        timeout: float,
    ) -> tuple[list[FakeProcess], list[FakeProcess]]:
        nonlocal wait_calls
        assert timeout > 0
        wait_calls += 1
        return ([], processes) if wait_calls == 1 else (processes, [])

    async def reap(run_id: str) -> int:
        return await reap_srt_runner_process_tree(
            run_id,
            process_iter=_process_iter([runner_process, child]),
            wait_procs=cast(WaitProcs, wait_for_exit),
            sandbox_root=sandbox_root,
        )

    monkeypatch.setattr(terminal_cleanup, "reap_srt_runner_process_tree", reap)
    _stub_runtime_cleanup(monkeypatch)
    caplog.set_level(logging.INFO, logger="gobby.agents.srt_process_cleanup")

    await _handler(RecordingDb()).post_terminal_cleanup(
        _run(status="error"),
        allow_parent_session_fallback=False,
    )

    assert runner_process.terminated is True
    assert child.terminated is True
    assert runner_process.killed is True
    assert child.killed is True
    assert "run_id=run-1 pid_count=2" in caplog.text


@pytest.mark.asyncio
async def test_startup_sweep_reaps_orphan_and_spares_live_run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    live_child = FakeProcess(202, ["claude"])
    live_runner = _runner_process("live-run", 201, children=[live_child])
    orphan_child = FakeProcess(302, ["codex"])
    orphan_runner = _runner_process("orphan-run", 301, children=[orphan_child])
    unrelated = FakeProcess(401, ["node", "/tmp/unrelated/runner.mjs"])
    processes = [live_runner, live_child, orphan_runner, orphan_child, unrelated]

    class RunStorage:
        def list_active_for_machine(
            self,
            machine_id: str,
            *,
            limit: int,
            offset: int,
        ) -> list[SimpleNamespace]:
            del machine_id
            assert limit > 0
            return [SimpleNamespace(id="live-run")] if offset == 0 else []

    def reap(active_run_ids: set[str]) -> int:
        return reap_orphaned_srt_runner_process_trees(
            active_run_ids,
            process_iter=_process_iter(processes),
            wait_procs=cast(WaitProcs, _wait_procs),
            sandbox_root=gobby_home / "run" / "sandbox",
        )

    monkeypatch.setattr(
        "gobby.runner_lifecycle_agents.reap_orphaned_srt_runner_process_trees",
        reap,
    )
    caplog.set_level(logging.INFO, logger="gobby.agents.srt_process_cleanup")
    runner = SimpleNamespace(
        agent_runner=SimpleNamespace(run_storage=RunStorage()),
        db_executor=None,
    )

    reaped = await _reap_orphaned_srt_runners_on_startup(cast("GobbyRunner", runner))

    assert reaped == 2
    assert orphan_runner.terminated is True
    assert orphan_child.terminated is True
    assert live_runner.terminated is False
    assert live_child.terminated is False
    assert unrelated.terminated is False
    assert "run_id=orphan-run pid_count=2" in caplog.text
    assert "run_id=live-run" not in caplog.text
