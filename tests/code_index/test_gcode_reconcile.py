"""gcode ``vector reconcile`` argv: a dry run by default, apply only with a digest and receipt."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult, GcodeGateway

pytestmark = pytest.mark.unit

BINARY = "/bin/gcode"


class _RecordingGateway(GcodeGateway):
    """Records the argv that would run instead of spawning gcode."""

    def __init__(self) -> None:
        super().__init__(binary=BINARY)
        self.commands: list[tuple[str, ...]] = []

    async def _ensure_version(self) -> str:
        return BINARY

    async def _run_command_result(
        self,
        command,
        *,
        timeout=None,
        env=None,
    ) -> GcodeCommandResult:
        self.commands.append(tuple(command))
        return GcodeCommandResult(
            command=tuple(command),
            returncode=0,
            stdout="{}",
            stderr="",
            started_at="2026-11-08T02:00:00+00:00",
            completed_at="2026-11-08T02:00:01+00:00",
            duration_seconds=1.0,
            timeout_seconds=None,
        )


async def test_dry_run_argv_carries_no_apply_flags(tmp_path: Path) -> None:
    gateway = _RecordingGateway()

    await gateway.vector_reconcile(tmp_path)

    assert gateway.commands == [
        (BINARY, "vector", "reconcile", "--project", str(tmp_path), "--format", "json"),
    ]


async def test_apply_argv_carries_the_digest_and_the_receipt_path(tmp_path: Path) -> None:
    gateway = _RecordingGateway()
    receipt = tmp_path / "run.json"

    await gateway.vector_reconcile(
        tmp_path,
        apply=True,
        expect_digest="abc123",
        receipt_path=receipt,
    )

    assert gateway.commands == [
        (
            BINARY,
            "vector",
            "reconcile",
            "--project",
            str(tmp_path),
            "--format",
            "json",
            "--apply",
            "--expect-digest",
            "abc123",
            "--receipt",
            str(receipt),
        ),
    ]


async def test_apply_without_digest_or_receipt_never_reaches_gcode(tmp_path: Path) -> None:
    gateway = _RecordingGateway()

    with pytest.raises(ValueError):
        await gateway.vector_reconcile(tmp_path, apply=True)

    assert gateway.commands == []
