from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from gobby.agents import sandbox_reaper
from gobby.agents.sandbox_reaper import (
    SandboxReapResult,
    reap_sandbox_run_roots,
    reap_terminal_sandbox_run,
    sweep_sandbox_run_roots,
)

pytestmark = pytest.mark.unit

_NOW = 2_000_000_000.0


def _sandbox_root(gobby_home: Path, run_id: str) -> Path:
    return gobby_home / "run" / "sandbox" / run_id


def _managed_root(gobby_home: Path, run_id: str) -> Path:
    return gobby_home / "runtime" / "managed-executions" / run_id


def _create_root(root: Path, content: bytes = b"sandbox data") -> Path:
    root.mkdir(parents=True)
    (root / "payload").write_bytes(content)
    return root


def _set_age(path: Path, age_seconds: float) -> None:
    timestamp = _NOW - age_seconds
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


@pytest.mark.asyncio
async def test_terminal_sandbox_cleanup_reaps_processes_before_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_order: list[str] = []
    expected = SandboxReapResult(removed_roots=2, removed_bytes=42)

    async def reap_processes(run_id: str) -> int:
        assert run_id == "terminal-run"
        cleanup_order.append("processes")
        return 1

    async def reap_roots(run_id: str) -> SandboxReapResult:
        assert run_id == "terminal-run"
        cleanup_order.append("roots")
        return expected

    monkeypatch.setattr(sandbox_reaper, "reap_srt_runner_process_tree", reap_processes)
    monkeypatch.setattr(sandbox_reaper, "reap_sandbox_run_roots", reap_roots)

    result = await reap_terminal_sandbox_run("terminal-run")

    assert result == expected
    assert cleanup_order == ["processes", "roots"]


@pytest.mark.asyncio
async def test_terminal_run_roots_removed_and_violation_log_retained(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "terminal-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    managed_root = _create_root(_managed_root(gobby_home, run_id))
    violation_log = sandbox_root / "logs" / "violations.jsonl"
    violation_log.parent.mkdir()
    violation_log.write_text('{"operation":"read"}\n', encoding="utf-8")

    result = await reap_sandbox_run_roots(run_id, gobby_home=gobby_home)

    assert result.removed_roots == 2
    assert result.removed_bytes > 0
    assert not sandbox_root.exists()
    assert not managed_root.exists()
    retained = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl"
    assert retained.read_text(encoding="utf-8") == '{"operation":"read"}\n'


@pytest.mark.asyncio
async def test_startup_sweep_removes_terminal_roots_and_keeps_active_runs(
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    terminal_roots = [
        _create_root(_sandbox_root(gobby_home, "terminal-run")),
        _create_root(_managed_root(gobby_home, "terminal-run")),
    ]
    running_root = _create_root(_sandbox_root(gobby_home, "running-run"))
    pending_root = _create_root(_managed_root(gobby_home, "pending-run"))
    for root in (*terminal_roots, running_root, pending_root):
        _set_age(root, 7_200)

    result = await sweep_sandbox_run_roots(
        {"running-run", "pending-run"},
        gobby_home=gobby_home,
        now=_NOW,
    )

    assert result.removed_roots == 2
    assert all(not root.exists() for root in terminal_roots)
    assert running_root.exists()
    assert pending_root.exists()


@pytest.mark.asyncio
async def test_startup_sweep_removes_old_orphan_and_keeps_young_orphan(
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    old_orphan = _create_root(_sandbox_root(gobby_home, "old-orphan"))
    young_orphan = _create_root(_sandbox_root(gobby_home, "young-orphan"))
    _set_age(old_orphan, 3_601)
    _set_age(young_orphan, 3_599)

    result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)

    assert result.removed_roots == 1
    assert not old_orphan.exists()
    assert young_orphan.exists()


@pytest.mark.asyncio
async def test_startup_sweep_logs_one_info_summary(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    sandbox_root = _create_root(_sandbox_root(gobby_home, "orphan-run"))
    managed_root = _create_root(_managed_root(gobby_home, "orphan-run"))
    _set_age(sandbox_root, 7_200)
    _set_age(managed_root, 7_200)
    caplog.set_level(logging.INFO, logger="gobby.agents.sandbox_reaper")

    result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)

    records = [record for record in caplog.records if record.name == "gobby.agents.sandbox_reaper"]
    assert result.removed_roots == 2
    assert len(records) == 1
    assert records[0].getMessage().startswith("Reaped 2 sandbox run root(s) (")


@pytest.mark.asyncio
async def test_startup_sweep_noop_is_silent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.agents.sandbox_reaper")

    result = await sweep_sandbox_run_roots(set(), gobby_home=tmp_path, now=_NOW)

    records = [record for record in caplog.records if record.name == "gobby.agents.sandbox_reaper"]
    assert result.removed_roots == 0
    assert records == []


@pytest.mark.asyncio
async def test_reaper_does_not_follow_symlinks_outside_run_root(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    external_root = tmp_path / "external"
    external_root.mkdir()
    external_file = external_root / "keep.txt"
    external_file.write_text("keep", encoding="utf-8")
    run_root = _create_root(_sandbox_root(gobby_home, "orphan-run"))
    (run_root / "external-link").symlink_to(external_root, target_is_directory=True)
    _set_age(run_root, 7_200)
    linked_root = _managed_root(gobby_home, "linked-run")
    linked_root.parent.mkdir(parents=True)
    linked_root.symlink_to(external_root, target_is_directory=True)
    _set_age(linked_root, 7_200)

    result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)

    assert result.removed_roots == 2
    assert not run_root.exists()
    assert not linked_root.is_symlink()
    assert external_file.read_text(encoding="utf-8") == "keep"
