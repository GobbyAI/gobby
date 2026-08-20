"""Tests for the gcode graph gateway."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeDaemonConfigUnavailableError,
    GcodeFalkorTransportError,
    GcodeGateway,
    GcodeIndexedFileNotFoundError,
    GcodeInputValidationError,
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


async def test_managed_gcode_pin_requires_1_5_0() -> None:
    assert GCODE_PIN == "1.5.0"


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b'{"success": true}',
        stderr: bytes = b"",
        timeout: bool = False,
        cancelled: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout
        self.cancelled = cancelled
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.cancelled:
            raise asyncio.CancelledError
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
        ),
    ]
    assert gateway.checked_version == GCODE_PIN


async def test_gateway_forwards_success_stderr_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"status": "ok"}', stderr=b"gcode diagnostic\n"),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    assert await gateway.graph_sync_file(tmp_path, "src/app.py") == {"status": "ok"}

    assert capsys.readouterr().err == "gcode diagnostic\n"


async def test_gateway_forwards_sync_file_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"status": "ok"}'),
        FakeProcess(stdout=b'{"success": true, "file": "src/app.py"}'),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    timeouts: list[float | None] = []

    async def fake_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    gateway = GcodeGateway(binary="/tmp/gcode", timeout_seconds=7.0)

    assert await gateway.graph_sync_file(tmp_path, "src/app.py", timeout=31.0) == {"status": "ok"}
    assert await gateway.vector_sync_file(tmp_path, "src/app.py", timeout=32.0) == {
        "success": True,
        "file": "src/app.py",
    }

    assert timeouts == [7.0, 31.0, 32.0]
    assert calls[1:] == [
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
        ),
        (
            "/tmp/gcode",
            "vector",
            "sync-file",
            "--file",
            "src/app.py",
            "--project",
            str(tmp_path),
            "--allow-missing-indexed-file",
            "--format",
            "json",
        ),
    ]


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
        ),
        (
            "/tmp/gcode",
            "graph",
            "rebuild",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ),
    ]


async def test_gateway_rejects_option_like_project_id_on_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_subprocess(monkeypatch, [])
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeInputValidationError, match="value must not start with '-'"):
        await gateway.graph_clear("--help")
    with pytest.raises(GcodeInputValidationError, match="value must not start with '-'"):
        await gateway.vector_clear(project_id="--help")

    assert calls == []


async def test_gateway_builds_incremental_index_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b"indexed"),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    result = await gateway.incremental_index(
        tmp_path,
        ["src/app.py", "docs/readme.md"],
        timeout=11,
    )

    assert result.success is True
    assert calls[1] == (
        "/tmp/gcode",
        "index",
        "--project",
        str(tmp_path),
        "--files",
        "src/app.py",
        "docs/readme.md",
        "--quiet",
        "--skip-if-locked",
    )
    assert result.timeout_seconds == 11


async def test_gateway_builds_vector_and_prune_args_with_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(stdout=b'{"success": true, "file": "src/app.py"}'),
        FakeProcess(stdout=b'{"success": true, "cleared": true}'),
        FakeProcess(stdout=b'{"success": true, "rebuilt": true}'),
        FakeProcess(stdout=b"No stale projects found."),
        FakeProcess(stdout=b"indexed"),
        FakeProcess(stdout=b'{"indexed_files": 1}'),
        FakeProcess(stdout=b"targeted prune"),
        FakeProcess(stdout=b"invalidated"),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    timeouts: list[float | None] = []

    async def fake_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    gateway = GcodeGateway(binary="/tmp/gcode", timeout_seconds=7.0, rebuild_timeout_seconds=42.0)

    assert await gateway.vector_sync_file(tmp_path, "src/app.py") == {
        "success": True,
        "file": "src/app.py",
    }
    assert await gateway.vector_clear(tmp_path) == {"success": True, "cleared": True}
    assert await gateway.vector_rebuild(tmp_path) == {"success": True, "rebuilt": True}
    assert await gateway.prune(tmp_path) == {
        "success": True,
        "output": "No stale projects found.",
    }
    maintenance_result = await gateway.maintenance_index(tmp_path, timeout=11)
    nightly_result = await gateway.nightly_repair(tmp_path, timeout=12)
    targeted_prune_result = await gateway.prune_project_for_maintenance(
        tmp_path, retention_days=45, timeout=14
    )
    invalidate_result = await gateway.invalidate_project_by_id("project-1", timeout=15)

    assert maintenance_result.success is True
    assert nightly_result.success is True
    assert targeted_prune_result.success is True
    assert invalidate_result.success is True
    assert timeouts == [7.0, 7.0, 42.0, 42.0, 42.0, 11, 12, 14, 15]
    assert calls[1:] == [
        (
            "/tmp/gcode",
            "vector",
            "sync-file",
            "--file",
            "src/app.py",
            "--project",
            str(tmp_path),
            "--allow-missing-indexed-file",
            "--format",
            "json",
        ),
        (
            "/tmp/gcode",
            "vector",
            "clear",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ),
        (
            "/tmp/gcode",
            "vector",
            "rebuild",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ),
        (
            "/tmp/gcode",
            "prune",
            "--force",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ),
        (
            "/tmp/gcode",
            "index",
            "--project",
            str(tmp_path),
            "--skip-if-locked",
        ),
        (
            "/tmp/gcode",
            "repair",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ),
        (
            "/tmp/gcode",
            "prune",
            "--force",
            "--project",
            str(tmp_path),
            "--retention-days",
            "45",
        ),
        (
            "/tmp/gcode",
            "invalidate",
            "--project-id",
            "project-1",
            "--force",
        ),
    ]


@pytest.mark.asyncio
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
        FakeProcess(stdout=b'{"path": []}'),
    ]
    calls = _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    await gateway.graph_file(tmp_path, "src/app.py")
    await gateway.graph_neighbors(tmp_path, "sym-1", limit=7)
    await gateway.graph_blast_radius(tmp_path, symbol_id="sym-1", depth=2, limit=9)
    await gateway.graph_blast_radius(tmp_path, file_path="src/app.py", depth=4, limit=11)
    await gateway.symbol_path(tmp_path, "from-symbol", "to-symbol", 8)

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
        ),
        (
            "/tmp/gcode",
            "path",
            "from-symbol",
            "to-symbol",
            "--project",
            str(tmp_path),
            "--max-depth",
            "8",
            "--format",
            "json",
        ),
    ]


@pytest.mark.parametrize(
    ("invalid_value", "expected_reason"),
    [
        ("-src/app.py", "value must not start with '-'"),
        ("/tmp/app.py", "value must not be an absolute path"),
        ("src/../app.py", "value must not contain '..' segments"),
    ],
)
@pytest.mark.parametrize("operation", ["sync_file", "graph_file", "blast_radius"])
async def test_gateway_rejects_invalid_file_paths_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    invalid_value: str,
    expected_reason: str,
) -> None:
    calls = _patch_subprocess(monkeypatch, [])
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeInputValidationError, match=expected_reason):
        if operation == "sync_file":
            await gateway.graph_sync_file(tmp_path, invalid_value)
        elif operation == "graph_file":
            await gateway.graph_file(tmp_path, invalid_value)
        else:
            await gateway.graph_blast_radius(tmp_path, file_path=invalid_value)

    assert calls == []


@pytest.mark.parametrize(
    ("invalid_value", "expected_reason"),
    [
        ("-sym-1", "value must not start with '-'"),
        ("/sym-1", "value must not be an absolute path"),
        ("symbols/../sym-1", "value must not contain '..' segments"),
    ],
)
@pytest.mark.parametrize("operation", ["neighbors", "blast_radius"])
async def test_gateway_rejects_invalid_symbol_ids_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    invalid_value: str,
    expected_reason: str,
) -> None:
    calls = _patch_subprocess(monkeypatch, [])
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeInputValidationError, match=expected_reason):
        if operation == "neighbors":
            await gateway.graph_neighbors(tmp_path, invalid_value)
        else:
            await gateway.graph_blast_radius(tmp_path, symbol_id=invalid_value)

    assert calls == []


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


async def test_gateway_cleans_up_process_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled_proc = FakeProcess(cancelled=True)
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        cancelled_proc,
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(asyncio.CancelledError):
        await gateway.graph_clear("proj-1")

    assert cancelled_proc.killed is True
    assert cancelled_proc.waited is True


async def test_gateway_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.code_index.gcode_gateway.resolve_native_bin", lambda _name: None)
    gateway = GcodeGateway()

    with pytest.raises(GcodeUnavailableError, match="gcode is not installed"):
        await gateway.graph_clear("proj-1")


async def test_gateway_raises_for_nonzero_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(returncode=2, stderr=b"boom"),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeCommandError, match="gcode exited 2: boom"):
        await gateway.graph_clear("proj-1")

    assert capsys.readouterr().err == ""


async def test_gateway_classifies_daemon_config_transport_without_forwarding_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = (
        b"Error: daemon effective config request failed: daemon could not be reached (unreachable)"
    )
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(returncode=1, stderr=stderr),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeDaemonConfigUnavailableError, match="daemon could not be reached"):
        await gateway.graph_clear("proj-1")

    assert capsys.readouterr().err == ""


async def test_maintenance_command_classifies_daemon_config_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    stderr = b"Error: daemon effective config request failed: daemon could not be reached (timeout)"
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(returncode=1, stderr=stderr),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeDaemonConfigUnavailableError):
        await gateway.prune_project_for_maintenance(tmp_path, retention_days=30)

    assert capsys.readouterr().err == ""


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


async def test_gateway_classifies_current_project_not_found_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(
            returncode=1,
            stderr=b"No gcode project found. Run `gcode init` to initialize this directory.",
        ),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeProjectNotFoundError) as exc_info:
        await gateway.graph_sync_file(tmp_path, "src/app.py")

    assert exc_info.value.project_path == str(tmp_path)
    assert exc_info.value.returncode == 1


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


async def test_gateway_classifies_falkor_eagain_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(
            returncode=1,
            stderr=b"Error: FalkorDB graph query failed: Resource temporarily unavailable (os error 35)",
        ),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeFalkorTransportError, match="os error 35"):
        await gateway.graph_sync_file(tmp_path, "src/app.py")


async def test_gateway_classifies_unrelated_graph_stderr_as_command_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = [
        FakeProcess(stdout=GCODE_PIN_STDOUT),
        FakeProcess(returncode=1, stderr=b"Error: FalkorDB graph query failed: syntax error"),
    ]
    _patch_subprocess(monkeypatch, processes)
    gateway = GcodeGateway(binary="/tmp/gcode")

    with pytest.raises(GcodeCommandError, match="syntax error") as exc_info:
        await gateway.graph_sync_file(tmp_path, "src/app.py")

    assert type(exc_info.value) is GcodeCommandError


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
