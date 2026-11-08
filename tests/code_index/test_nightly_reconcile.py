"""Nightly vector reconcile: dry run by default, guarded apply behind the config switch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult
from gobby.code_index.nightly_reconcile import (
    MODE_APPLY,
    MODE_DRY_RUN,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    NightlyReconcileReceipt,
    reconcile_project,
)

pytestmark = pytest.mark.unit


def _result(payload: Mapping[str, Any] | None, *, returncode: int = 0) -> GcodeCommandResult:
    stdout = json.dumps(payload) if payload is not None else ""
    return GcodeCommandResult(
        command=("gcode",),
        returncode=returncode,
        stdout=stdout,
        stderr="" if returncode == 0 else "read error",
        started_at="2026-11-08T02:00:00+00:00",
        completed_at="2026-11-08T02:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=None,
    )


def _failed_read() -> GcodeCommandResult:
    return _result(None, returncode=1)


def _dry(
    *,
    points: int = 10,
    symbols: int = 10,
    orphans: int = 0,
    digest: str = "d1",
    missing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "mode": "dry_run",
        "points_scanned": points,
        "pg_symbols": symbols,
        "orphans": {"stale": {"count": orphans, "ids": []}},
        "missing": missing or {},
        "inventory_digest": digest,
    }


def _persist_progress_receipt(receipt_path: Path, digest: str) -> str | None:
    """Apply gcode's receipt rule (reconcile.rs apply_with_persist) to a scripted apply.

    An existing receipt at the path must belong to the same inventory digest. The first progress
    write lands before any deletion, so a failed apply leaves its receipt on disk.
    """
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        if previous.get("digest") != digest:
            return f"receipt {receipt_path} belongs to another reconcile run"
    progress = {"digest": digest, "project_id": "project-1", "complete": False}
    receipt_path.write_text(json.dumps(progress), encoding="utf-8")
    return None


def _applied(digest: str, deleted: int = 0) -> GcodeCommandResult:
    return _result(
        {
            "success": True,
            "mode": "apply",
            "inventory_digest": digest,
            "deleted_points": deleted,
            "versions_reset": 0,
            "complete": True,
        }
    )


class ScriptedGateway:
    """Replays scripted gcode results and records every call in order."""

    def __init__(
        self,
        *,
        dry_runs: list[GcodeCommandResult],
        applies: list[GcodeCommandResult] | None = None,
    ) -> None:
        self._dry_runs = list(dry_runs)
        self._applies = list(applies or [])
        self.calls: list[str] = []
        self.apply_digests: list[str | None] = []
        self.apply_receipt_paths: list[Path | None] = []
        self.receipt_refusals: list[str] = []
        self.index_calls = 0
        self.rebuild_calls = 0
        self.rebuild_timeouts: list[float | None] = []
        self.rebuild_envs: list[Mapping[str, str] | None] = []

    async def vector_reconcile(
        self,
        project_root: Path,
        *,
        apply: bool = False,
        expect_digest: str | None = None,
        receipt_path: Path | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        if apply:
            self.calls.append("apply")
            self.apply_digests.append(expect_digest)
            self.apply_receipt_paths.append(receipt_path)
            if receipt_path is not None and expect_digest is not None:
                refusal = _persist_progress_receipt(receipt_path, expect_digest)
                if refusal is not None:
                    self.receipt_refusals.append(refusal)
                    return _result(None, returncode=1)
            return self._applies.pop(0)
        self.calls.append("dry")
        return self._dry_runs.pop(0)

    async def maintenance_index(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        self.calls.append("index")
        self.index_calls += 1
        return _result({"success": True})

    async def vector_rebuild(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append("rebuild")
        self.rebuild_calls += 1
        self.rebuild_timeouts.append(timeout)
        self.rebuild_envs.append(env)
        return {"success": True}


async def _run(
    gateway: ScriptedGateway,
    tmp_path: Path,
    *,
    apply_enabled: bool,
    max_attempts: int = 3,
) -> NightlyReconcileReceipt:
    return await reconcile_project(
        run_id="run-1",
        project_id="project-1",
        root=tmp_path,
        gateway=gateway,
        apply_enabled=apply_enabled,
        receipt_dir=tmp_path / "receipts",
        max_attempts=max_attempts,
    )


async def test_key_off_dry_run_records_counts_and_never_applies(tmp_path: Path) -> None:
    gateway = ScriptedGateway(dry_runs=[_result(_dry(points=10, symbols=10, orphans=3))])

    receipt = await _run(gateway, tmp_path, apply_enabled=False)

    assert receipt.mode == MODE_DRY_RUN
    assert receipt.outcome == OUTCOME_COMPLETED
    assert receipt.digest == "d1"
    assert receipt.retries == 0
    assert receipt.error is None
    assert receipt.class_counts == {
        "points_scanned": 10,
        "pg_symbols": 10,
        "orphans": {"stale": 3},
        "missing": {},
    }
    assert gateway.apply_digests == []


async def test_key_on_applies_observed_digest_without_volume_cap(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(points=9000, symbols=9000, orphans=5000, digest="obs"))],
        applies=[_applied("obs", deleted=5000)],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert gateway.apply_digests == ["obs"]
    assert receipt.mode == MODE_APPLY
    assert receipt.outcome == OUTCOME_COMPLETED
    assert receipt.digest == "obs"
    assert receipt.class_counts["orphans"] == {"stale": 5000}


async def test_apply_receipt_file_lives_in_the_private_receipt_dir(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(digest="obs"))],
        applies=[_applied("obs")],
    )

    await _run(gateway, tmp_path, apply_enabled=True)

    [receipt_path] = gateway.apply_receipt_paths
    assert receipt_path is not None
    assert receipt_path.parent == tmp_path / "receipts"


async def test_apply_receipts_are_separate_per_project_within_one_run(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(digest="a")), _result(_dry(digest="b"))],
        applies=[_applied("a"), _applied("b")],
    )

    for project_id in ("project-a", "project-b"):
        await reconcile_project(
            run_id="run-1",
            project_id=project_id,
            root=tmp_path / project_id,
            gateway=gateway,
            apply_enabled=True,
            receipt_dir=tmp_path / "receipts",
        )

    first, second = gateway.apply_receipt_paths
    assert first is not None
    assert second is not None
    assert first != second


async def test_failed_read_retries_then_records_failure_and_never_deletes(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_failed_read(), _failed_read(), _failed_read()],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True, max_attempts=3)

    assert receipt.outcome == OUTCOME_FAILED
    assert receipt.retries == 2
    assert receipt.error is not None
    assert "read error" in receipt.error
    assert gateway.apply_digests == []


async def test_failed_read_then_success_retries_and_applies(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_failed_read(), _result(_dry(digest="ok"))],
        applies=[_applied("ok")],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert receipt.outcome == OUTCOME_COMPLETED
    assert receipt.retries == 1
    assert gateway.apply_digests == ["ok"]


async def test_failed_apply_retries_from_a_fresh_dry_run(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(digest="a")), _result(_dry(digest="b"))],
        applies=[_failed_read(), _applied("b")],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert gateway.receipt_refusals == []
    assert receipt.outcome == OUTCOME_COMPLETED
    assert receipt.retries == 1
    assert gateway.apply_digests == ["a", "b"]
    assert receipt.digest == "b"
    first, second = gateway.apply_receipt_paths
    assert first is not None
    assert second is not None
    assert first != second
    assert first.exists(), "the failed attempt's receipt is audit evidence and must stay"


async def test_empty_vector_collection_is_re_embedded_before_apply(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[
            _result(_dry(points=0, symbols=5, digest="empty")),
            _result(_dry(points=5, symbols=5, digest="full")),
        ],
        applies=[_applied("full")],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert gateway.rebuild_calls == 1
    assert gateway.index_calls == 0
    assert gateway.calls == ["dry", "rebuild", "dry", "apply"]
    assert gateway.apply_digests == ["full"]
    assert receipt.outcome == OUTCOME_COMPLETED


async def test_empty_symbol_table_is_indexed_before_reconcile(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[
            _result(_dry(points=0, symbols=0, digest="none")),
            _result(_dry(points=7, symbols=7, digest="built")),
        ],
        applies=[_applied("built")],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert gateway.index_calls == 1
    assert gateway.rebuild_calls == 0
    assert gateway.calls == ["dry", "index", "dry", "apply"]
    assert gateway.apply_digests == ["built"]
    assert receipt.outcome == OUTCOME_COMPLETED


async def test_empty_table_and_collection_are_both_repaired_in_one_attempt(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[
            _result(_dry(points=0, symbols=0, digest="none")),
            _result(_dry(points=0, symbols=7, digest="indexed")),
            _result(_dry(points=7, symbols=7, digest="full")),
        ],
        applies=[_applied("full")],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=True)

    assert gateway.calls == ["dry", "index", "dry", "rebuild", "dry", "apply"]
    assert gateway.apply_digests == ["full"]
    assert receipt.retries == 0
    assert receipt.outcome == OUTCOME_COMPLETED


async def test_dry_run_never_runs_the_empty_input_guards(tmp_path: Path) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(points=0, symbols=0, digest="none"))],
    )

    receipt = await _run(gateway, tmp_path, apply_enabled=False)

    assert gateway.index_calls == 0
    assert gateway.rebuild_calls == 0
    assert gateway.apply_digests == []
    assert receipt.mode == MODE_DRY_RUN
    assert receipt.outcome == OUTCOME_COMPLETED


async def test_missing_classes_keep_their_versions_and_symbols(tmp_path: Path) -> None:
    """gcode reports a missing class as versions, symbols, version_ids (reconcile.rs)."""
    missing = {
        "synced_missing_current": {"versions": 2, "symbols": 5, "version_ids": ["v1", "v2"]},
        "pending_current": {"versions": 0, "symbols": 0, "version_ids": []},
    }
    gateway = ScriptedGateway(dry_runs=[_result(_dry(missing=missing))])

    receipt = await _run(gateway, tmp_path, apply_enabled=False)

    assert receipt.class_counts["missing"] == {
        "synced_missing_current": {"versions": 2, "symbols": 5},
        "pending_current": {"versions": 0, "symbols": 0},
    }


async def test_empty_collection_rebuild_receives_the_maintenance_env_and_timeout(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway(
        dry_runs=[_result(_dry(points=0, digest="d1")), _result(_dry(digest="d1"))],
        applies=[_applied("d1")],
    )
    env = {"GOBBY_TEST_GRANT": "grant"}

    receipt = await reconcile_project(
        run_id="run-1",
        project_id="project-1",
        root=tmp_path,
        gateway=gateway,
        apply_enabled=True,
        receipt_dir=tmp_path / "receipts",
        timeout=9.0,
        env=env,
    )

    assert receipt.outcome == OUTCOME_COMPLETED
    assert gateway.rebuild_timeouts == [9.0]
    assert gateway.rebuild_envs == [env]
