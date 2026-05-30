"""Tests for the gcode graph gateway."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeGateway,
    GcodeIndexedFileNotFoundError,
    GcodeJsonError,
    GcodeProjectNotFoundError,
    GcodeTimeoutError,
    GcodeUnavailableError,
    GcodeVersionError,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
GCODE_PIN = MANAGED_BIN_VERSION_PINS["gcode"]
GCODE_PIN_STDOUT = f"gcode {GCODE_PIN}\n".encode()


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b'{"success": true}',
        stderr: bytes = b"",
        timeout: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.timeout:
            raise TimeoutError
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        self.waited = True


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[FakeProcess],
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def fake_create_subprocess_exec(*args: Any, **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    return calls


async def test_gateway_checks_version_once_and_builds_sync_file_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"status": "ok"}'),
        FakeProcess(stdout=b'{"nodes": []}'),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    assert await gateway.graph_sync_file(tmp_path, "src/app.py") == {"status": "ok"}
    assert await gateway.graph_overview(tmp_path, limit=25) == {"nodes": []}

    assert calls == [
        ("/tmp/gcode", "--version"),
        (
            "/tmp/gcode",
            "graph",
            "sync-file",
            "--file",
            "src/app.py",
            "--project",
            str(tmp_path),
            "--allow-missing-indexed-file",
            "--format",
            "json",
            "--quiet",
        ),
        (
            "/tmp/gcode",
            "graph",
            "overview",
            "--project",
            str(tmp_path),
            "--limit",
            "25",
            "--format",
            "json",
            "--quiet",
        ),
    ]
    assert gateway.checked_version == GCODE_PIN


async def test_gateway_builds_clear_and_rebuild_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"success": true}'),
        FakeProcess(stdout=b'{"success": true}'),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    await gateway.graph_clear("proj-1")
    await gateway.graph_rebuild(tmp_path)

    assert calls[1:] == [
        (
            "/tmp/gcode",
            "graph",
            "clear",
            "--project-id",
            "proj-1",
            "--format",
            "json",
            "--quiet",
        ),
        (
            "/tmp/gcode",
            "graph",
            "rebuild",
            "--project",
            str(tmp_path),
            "--format",
            "json",
            "--quiet",
        ),
    ]


async def test_gateway_builds_graph_read_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"nodes": []}'),
        FakeProcess(stdout=b'{"nodes": []}'),
        FakeProcess(stdout=b'{"nodes": []}'),
        FakeProcess(stdout=b'{"nodes": []}'),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    await gateway.graph_file(tmp_path, "src/app.py")
    await gateway.graph_neighbors(tmp_path, "sym-1", limit=7)
    await gateway.graph_blast_radius(tmp_path, symbol_id="sym-1", depth=2, limit=9)
    await gateway.graph_blast_radius(tmp_path, file_path="src/app.py", depth=4, limit=11)

    assert calls[1:] == [
        (
            "/tmp/gcode",
            "graph",
            "file",
            "--file",
            "src/app.py",
            "--project",
            str(tmp_path),
            "--format",
            "json",
            "--quiet",
        ),
        (
            "/tmp/gcode",
            "graph",
            "neighbors",
            "--symbol-id",
            "sym-1",
            "--project",
            str(tmp_path),
            "--limit",
            "7",
            "--format",
            "json",
            "--quiet",
        ),
        (
            "/tmp/gcode",
            "graph",
            "blast-radius",
            "--project",
            str(tmp_path),
            "--symbol-id",
            "sym-1",
            "--depth",
            "2",
            "--limit",
            "9",
            "--format",
            "json",
            "--quiet",
        ),
        (
            "/tmp/gcode",
            "graph",
            "blast-radius",
            "--project",
            str(tmp_path),
            "--file",
            "src/app.py",
            "--depth",
            "4",
            "--limit",
            "11",
            "--format",
            "json",
            "--quiet",
        ),
    ]


async def test_gateway_rejects_blast_radius_without_target(tmp_path: Path) -> None:
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(ValueError, match="exactly one"):
        await gateway.graph_blast_radius(tmp_path)


async def test_gateway_rejects_blast_radius_with_multiple_targets(tmp_path: Path) -> None:
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(ValueError, match="exactly one"):
        await gateway.graph_blast_radius(tmp_path, symbol_id="sym-1", file_path="src/app.py")


async def test_gateway_rejects_stale_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, [FakeProcess(stdout=b"gcode 0.0.0\n")])
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeVersionError, match=f"gcode >= {GCODE_PIN} required"):
        await gateway.graph_clear("proj-1")


async def test_gateway_raises_for_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b"not-json"),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeJsonError, match="invalid JSON"):
        await gateway.graph_clear("proj-1")


async def test_gateway_raises_for_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeout_proc = FakeProcess(timeout=True)
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        timeout_proc,
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode", timeout_seconds=0.01)

    with pytest.raises(GcodeTimeoutError, match="gcode timed out"):
        await gateway.graph_clear("proj-1")

    assert timeout_proc.killed is True
    assert timeout_proc.waited is True


async def test_gateway_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.code_index.gcode_gateway.resolve_native_bin", lambda _name: None)
    gateway = GcodeGateway()

    with pytest.raises(GcodeUnavailableError, match="gcode is not installed"):
        await gateway.graph_clear("proj-1")


async def test_gateway_raises_for_nonzero_command(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(returncode=2, stderr=b"boom"),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeCommandError, match="gcode exited 2: boom"):
        await gateway.graph_clear("proj-1")


async def test_gateway_classifies_project_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(
            returncode=2,
            stderr=f"Project '{tmp_path}' not found".encode(),
        ),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeProjectNotFoundError) as exc_info:
        await gateway.graph_sync_file(tmp_path, "src/app.py")

    assert exc_info.value.project_path == str(tmp_path)
    assert exc_info.value.returncode == 2


async def test_gateway_classifies_indexed_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(
            returncode=2,
            stderr=b"indexed file `src/app.py` was not found for project proj-1",
        ),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeIndexedFileNotFoundError) as exc_info:
        await gateway.graph_sync_file(tmp_path, "src/app.py")

    assert exc_info.value.file_path == "src/app.py"
    assert exc_info.value.project_id == "proj-1"
    assert exc_info.value.returncode == 2


async def test_gateway_returns_indexed_file_not_found_skip_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"status": "skipped", "reason": "indexed_file_not_found"}'),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    assert await gateway.graph_sync_file(tmp_path, "src/app.py") == {
        "status": "skipped",
        "reason": "indexed_file_not_found",
    }
