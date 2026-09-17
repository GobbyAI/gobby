from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.agents import sandbox_reaper
from gobby.agents.sandbox_policy import (
    PRE_COMMIT_STORE_SPARE_NAME,
    PRE_COMMIT_STORE_SPARE_TEMP_NAME,
)
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


def _write_violation_log(sandbox_root: Path, content: str) -> Path:
    violation_log = sandbox_root / "logs" / "violations.jsonl"
    violation_log.parent.mkdir(parents=True)
    violation_log.write_text(content, encoding="utf-8")
    return violation_log


def _write_settings_document(sandbox_root: Path, content: str) -> Path:
    settings = sandbox_root / "assets" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(content, encoding="utf-8")
    return settings


@pytest.mark.asyncio
async def test_terminal_run_roots_removed_and_violation_log_retained(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "terminal-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    managed_root = _create_root(_managed_root(gobby_home, run_id))
    _write_violation_log(sandbox_root, '{"operation":"read"}\n')
    _write_settings_document(sandbox_root, '{"policy":"resolved"}\n')

    result = await reap_sandbox_run_roots(run_id, gobby_home=gobby_home)

    assert result.removed_roots == 2
    assert result.removed_bytes > 0
    assert not sandbox_root.exists()
    assert not managed_root.exists()
    retained = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl"
    retained_settings = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.settings.json"
    assert retained.read_text(encoding="utf-8") == '{"operation":"read"}\n'
    assert retained_settings.read_text(encoding="utf-8") == '{"policy":"resolved"}\n'
    assert retained_settings.stat().st_mode & 0o777 == 0o600
    assert result.retained_violation_log == retained
    assert result.retained_settings == retained_settings
    assert result.retention_metadata() == {
        "retained_violation_path": str(retained),
        "retained_settings_path": str(retained_settings),
    }


@pytest.mark.asyncio
async def test_reap_without_violations_reports_no_retained_artifacts(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "clean-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    _write_settings_document(sandbox_root, '{"policy":"resolved"}\n')

    result = await reap_sandbox_run_roots(run_id, gobby_home=gobby_home)

    assert result.removed_roots == 1
    assert not sandbox_root.exists()
    assert result.retained_violation_log is None
    assert result.retained_settings is None
    assert result.retention_metadata() == {}
    assert not (gobby_home / "logs" / "sandbox-violations").exists()


@pytest.mark.asyncio
async def test_reap_with_violations_but_no_settings_retains_log_only(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "no-settings-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    _write_violation_log(sandbox_root, '{"operation":"read"}\n')

    result = await reap_sandbox_run_roots(run_id, gobby_home=gobby_home)

    retained = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl"
    assert result.removed_roots == 1
    assert not sandbox_root.exists()
    assert result.retained_violation_log == retained
    assert result.retained_settings is None
    assert result.retention_metadata() == {"retained_violation_path": str(retained)}
    assert not (gobby_home / "logs" / "sandbox-violations" / f"{run_id}.settings.json").exists()


@pytest.mark.asyncio
async def test_settings_retention_failure_skips_root_and_reports_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "failed-retention-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    _write_violation_log(sandbox_root, '{"operation":"read"}\n')
    _write_settings_document(sandbox_root, '{"policy":"resolved"}\n')
    retain_artifact = sandbox_reaper._retain_run_artifact

    def fail_settings_retention(
        source: Path,
        run_id: str,
        filename: str,
        gobby_home: Path,
    ) -> Path:
        if filename.endswith(".settings.json"):
            raise OSError("retention denied")
        return retain_artifact(source, run_id, filename, gobby_home)

    monkeypatch.setattr(sandbox_reaper, "_retain_run_artifact", fail_settings_retention)

    result = await reap_sandbox_run_roots(run_id, gobby_home=gobby_home)

    assert result.skipped_roots == 1
    assert sandbox_root.exists()
    assert result.retained_violation_log is None
    assert result.retained_settings is None
    assert result.retention_metadata() == {}


def test_retention_metadata_patches_sandbox_record_keys() -> None:
    home = Path("/gobby-home")
    result = SandboxReapResult(
        retained_violation_log=home / "logs" / "sandbox-violations" / "run.jsonl",
        retained_settings=home / "logs" / "sandbox-violations" / "run.settings.json",
    )

    assert result.retention_metadata() == {
        "retained_violation_path": str(home / "logs" / "sandbox-violations" / "run.jsonl"),
        "retained_settings_path": str(home / "logs" / "sandbox-violations" / "run.settings.json"),
    }
    assert SandboxReapResult().retention_metadata() == {}


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
async def test_startup_sweep_records_retained_artifacts_per_run(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "orphan-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    _write_violation_log(sandbox_root, '{"operation":"write"}\n')
    _write_settings_document(sandbox_root, '{"policy":"resolved"}\n')
    _set_age(sandbox_root, 7_200)
    recorded: list[tuple[str, dict[str, str]]] = []

    def record_retention(run_id: str, patch: dict[str, str]) -> None:
        recorded.append((run_id, patch))

    result = await sweep_sandbox_run_roots(
        set(),
        gobby_home=gobby_home,
        now=_NOW,
        record_retention=record_retention,
    )

    retained_log = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl"
    retained_settings = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.settings.json"
    assert result.removed_roots == 1
    assert not sandbox_root.exists()
    assert recorded == [
        (
            run_id,
            {
                "retained_violation_path": str(retained_log),
                "retained_settings_path": str(retained_settings),
            },
        )
    ]


@pytest.mark.asyncio
async def test_startup_sweep_survives_retention_record_failure(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "record-failure-run"
    sandbox_root = _create_root(_sandbox_root(gobby_home, run_id))
    _write_violation_log(sandbox_root, '{"operation":"write"}\n')
    _set_age(sandbox_root, 7_200)

    def record_retention(run_id: str, patch: dict[str, str]) -> None:
        raise RuntimeError("record store unavailable")

    result = await sweep_sandbox_run_roots(
        set(),
        gobby_home=gobby_home,
        now=_NOW,
        record_retention=record_retention,
    )

    assert result.removed_roots == 1
    assert not sandbox_root.exists()
    assert (gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl").exists()


@pytest.mark.asyncio
async def test_startup_sweep_removes_read_only_entries(tmp_path: Path) -> None:
    gobby_home = tmp_path / "gobby-home"
    root = _create_root(_sandbox_root(gobby_home, "read-only-run"))
    read_only_directory = root / "read-only-directory"
    read_only_directory.mkdir()
    read_only_file = read_only_directory / "read-only-file"
    read_only_file.write_text("protected", encoding="utf-8")
    read_only_file.chmod(0o400)
    read_only_directory.chmod(0o500)
    _set_age(root, 7_200)

    try:
        result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)
    finally:
        if read_only_directory.exists():
            read_only_directory.chmod(0o700)
        if read_only_file.exists():
            read_only_file.chmod(0o600)

    assert result.removed_roots == 1
    assert result.skipped_roots == 0
    assert not root.exists()


@pytest.mark.asyncio
async def test_startup_sweep_skips_unremovable_root_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    blocked_root = _create_root(_sandbox_root(gobby_home, "blocked-run"))
    removable_root = _create_root(_sandbox_root(gobby_home, "removable-run"))
    _set_age(blocked_root, 7_200)
    _set_age(removable_root, 7_200)
    remove_root = sandbox_reaper._remove_root

    def fail_one_root(path: Path) -> bool:
        if path == blocked_root:
            return False
        return remove_root(path)

    monkeypatch.setattr(sandbox_reaper, "_remove_root", fail_one_root)
    caplog.set_level(logging.INFO, logger="gobby.agents.sandbox_reaper")

    result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)

    records = [record for record in caplog.records if record.name == "gobby.agents.sandbox_reaper"]
    info_records = [record for record in records if record.levelno == logging.INFO]
    warning_records = [record for record in records if record.levelno == logging.WARNING]
    assert result.removed_roots == 1
    assert result.skipped_roots == 1
    assert blocked_root.exists()
    assert not removable_root.exists()
    assert len(info_records) == 1
    assert "skipped=1" in info_records[0].getMessage()
    assert len(warning_records) == 1
    assert str(blocked_root) in warning_records[0].getMessage()


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
async def test_startup_sweep_removes_fresh_pre_commit_spare_and_partial_temp(
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    managed_root = gobby_home / "runtime" / "managed-executions"
    spare = _create_root(managed_root / PRE_COMMIT_STORE_SPARE_NAME)
    temporary = _create_root(managed_root / PRE_COMMIT_STORE_SPARE_TEMP_NAME)

    result = await sweep_sandbox_run_roots(set(), gobby_home=gobby_home, now=_NOW)

    assert result.removed_roots == 2
    assert not spare.exists()
    assert not temporary.exists()


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
    assert "skipped=0" in records[0].getMessage()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True])
async def test_reaper_removes_registered_short_tmp_and_preserves_other_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup: bool
) -> None:
    from gobby.agents.sandbox_policy import prepare_short_run_tmp

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    gobby_home = tmp_path / "long-gobby-home"
    root = _create_root(_managed_root(gobby_home, "finished-run"))
    active = _create_root(_managed_root(gobby_home, "active-run"))
    run_tmp = prepare_short_run_tmp(root)
    other_tmp = prepare_short_run_tmp(active)
    assert prepare_short_run_tmp(root) == run_tmp
    assert run_tmp != other_tmp
    assert run_tmp.stat().st_mode & 0o777 == 0o700
    nested = run_tmp / "nested"
    nested.mkdir()
    payload_size = 100_000
    (nested / "data").write_bytes(b"x" * payload_size)
    (nested / "escape").symlink_to(other_tmp, target_is_directory=True)
    # The writable temp directory cannot register another deletion target.
    (run_tmp / "tmp-path").write_text(str(other_tmp))
    (other_tmp / "keep").write_text("other run")
    _set_age(root, 7_200)
    _set_age(active, 7_200)

    if startup:
        result = await sweep_sandbox_run_roots({"active-run"}, gobby_home=gobby_home, now=_NOW)
    else:
        result = await reap_sandbox_run_roots("finished-run", gobby_home=gobby_home)

    assert result.removed_roots == 1
    assert result.skipped_roots == 0
    assert not root.exists()
    assert not run_tmp.exists()
    assert result.removed_bytes >= payload_size
    assert (other_tmp / "keep").read_text() == "other run"
    assert active.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["directory-link", "registration-link", "outside-path"])
async def test_short_tmp_registration_rejects_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    from gobby.agents.sandbox_policy import prepare_short_run_tmp

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    gobby_home = tmp_path / "gobby-home"
    root = _create_root(_managed_root(gobby_home, "finished-run"))
    run_tmp = prepare_short_run_tmp(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    registration = root / "tmp-path"
    if escape == "directory-link":
        run_tmp.rmdir()
        run_tmp.symlink_to(outside, target_is_directory=True)
    elif escape == "registration-link":
        target = outside / "registration"
        target.write_text(str(run_tmp))
        registration.unlink()
        registration.symlink_to(target)
    else:
        registration.write_text(str(outside))

    with pytest.raises(OSError, match="Invalid"):
        prepare_short_run_tmp(root)
    result = await reap_sandbox_run_roots("finished-run", gobby_home=gobby_home)

    assert result.skipped_roots == 1
    assert root.exists()
    assert (outside / "keep").read_text() == "keep"


def test_record_sandbox_retention_patches_the_run_sandbox_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class _Manager:
        def __init__(self, db: object) -> None:
            self.db = db

        def merge_sandbox_metadata(self, run_id: str, updates: dict[str, str]) -> None:
            calls.append((run_id, updates))

    monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", _Manager)
    retained = Path("/gobby-home/logs/sandbox-violations/run.jsonl")
    result = SandboxReapResult(retained_violation_log=retained)

    sandbox_reaper.record_sandbox_retention(cast(Any, object()), "run", result.retention_metadata())
    sandbox_reaper.record_sandbox_retention(
        cast(Any, object()), "clean-run", SandboxReapResult().retention_metadata()
    )

    assert calls == [("run", {"retained_violation_path": str(retained)})]


def test_record_sandbox_retention_survives_a_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Manager:
        def __init__(self, db: object) -> None:
            self.db = db

        def merge_sandbox_metadata(self, run_id: str, updates: dict[str, str]) -> None:
            raise RuntimeError("hub unavailable")

    monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", _Manager)
    caplog.set_level(logging.WARNING, logger="gobby.agents.sandbox_reaper")

    sandbox_reaper.record_sandbox_retention(
        cast(Any, object()),
        "run",
        {"retained_violation_path": "/gobby-home/logs/sandbox-violations/run.jsonl"},
    )

    assert "Failed to record retained sandbox diagnostics for run run" in caplog.text
