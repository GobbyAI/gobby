"""AGY version-gate foundation: one probe per executable identity."""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.ai import (
    AICapability,
    build_daemon_ai_capability_registry,
    build_daemon_tool_chat_service,
)
from gobby.config.app import DaemonConfig
from gobby.providers import AGY_UNAVAILABLE_REASON
from gobby.providers.version_gate import (
    AGY_REQUIRED_VERSION,
    AGY_REVALIDATING_REASON,
    AGY_UNPUBLISHED_REASON,
    AgySupportRecord,
    agy_support_is_published,
    assert_agy_support_published,
    ensure_agy_support,
    peek_agy_support,
    probe_and_publish_agy_support,
    reset_agy_support_for_tests,
)
from gobby.servers.provider_model_discovery import get_cli_version
from gobby.servers.routes.providers import _agy_snapshot_payload
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_agy_support() -> Iterator[None]:
    reset_agy_support_for_tests()
    yield
    reset_agy_support_for_tests()


def _install_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes = b"agy-v1",
) -> Path:
    binary = tmp_path / "bin" / "agy"
    binary.parent.mkdir()
    binary.write_bytes(payload)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return binary


def _replace_agy(path: Path, payload: bytes) -> None:
    replacement = path.with_name(f"{path.name}.new")
    replacement.write_bytes(payload)
    replacement.chmod(0o755)
    os.replace(replacement, path)


async def _yield_loop() -> None:
    resumed = asyncio.Event()
    asyncio.get_running_loop().call_soon(resumed.set)
    await resumed.wait()


def _identity_tuple(path: Path) -> tuple[str, int, int, int, int]:
    resolved = os.path.realpath(path)
    stat = os.stat(resolved)
    return (resolved, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@pytest.mark.asyncio
async def test_probe_publishes_immutable_support_record_readable_synchronously(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "gobby.providers.version_gate.get_cli_version",
        AsyncMock(return_value="agy 1.1.18"),
    )

    record = await probe_and_publish_agy_support()
    peeked = peek_agy_support()

    assert record.supported is True
    assert record.installed_version == "1.1.18"
    assert record.required_version == AGY_REQUIRED_VERSION == "1.1.18"
    assert peeked == record
    assert peeked.identity is not None
    assert peeked.identity.realpath == os.path.realpath(binary)
    assert inspect.iscoroutinefunction(probe_and_publish_agy_support)
    assert not inspect.iscoroutinefunction(peek_agy_support)
    with pytest.raises(FrozenInstanceError):
        type(record).__setattr__(record, "supported", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_output", "installed_label"),
    [
        ("agy 1.1.16", "1.1.16"),
        (None, "none"),
        ("agy from the future", "unparseable"),
    ],
    ids=["sub_floor", "absent_output", "unparseable"],
)
async def test_sub_floor_and_unparseable_records_name_installed_and_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_output: str | None,
    installed_label: str,
) -> None:
    _install_agy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "gobby.providers.version_gate.get_cli_version",
        AsyncMock(return_value=version_output),
    )

    record = await probe_and_publish_agy_support()

    assert record.supported is False
    assert AGY_REQUIRED_VERSION in record.reason
    assert installed_label in record.reason
    assert record.required_version == AGY_REQUIRED_VERSION


@pytest.mark.asyncio
async def test_absent_binary_is_unsupported_with_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    probe = AsyncMock(side_effect=AssertionError("absent binary must not spawn"))
    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", probe)

    record = await probe_and_publish_agy_support()

    probe.assert_not_awaited()
    assert record.supported is False
    assert record.installed_version is None
    assert "none" in record.reason
    assert AGY_REQUIRED_VERSION in record.reason
    assert peek_agy_support() == record


@pytest.mark.asyncio
async def test_sync_consumers_do_not_reprobe_when_identity_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_agy(tmp_path, monkeypatch)
    version = AsyncMock(return_value="1.1.18")
    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", version)

    await probe_and_publish_agy_support()
    version.assert_awaited_once()

    for _ in range(3):
        peeked = peek_agy_support()
        assert peeked.supported is True
        bindings = build_daemon_ai_capability_registry(
            DaemonConfig(),
            provider_installed=lambda _entry: True,
        )
        web_chat = bindings.binding(AICapability.WEB_CHAT, "agy")
        assert web_chat is not None
        assert web_chat.available is False
        payload = _agy_snapshot_payload()
        assert payload["support"]["supported"] is True
        await ensure_agy_support()

    version.assert_awaited_once()


def test_peek_returns_fail_closed_sentinel_before_publication() -> None:
    record = peek_agy_support()

    assert agy_support_is_published() is False
    assert record.supported is False
    assert record.reason == AGY_UNPUBLISHED_REASON
    assert record.required_version == AGY_REQUIRED_VERSION
    with pytest.raises(RuntimeError, match="version probe has not run"):
        assert_agy_support_published()


@pytest.mark.asyncio
async def test_record_carries_resolved_executable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "gobby.providers.version_gate.get_cli_version",
        AsyncMock(return_value="1.1.18"),
    )

    record = await probe_and_publish_agy_support()
    expected = _identity_tuple(binary)

    assert record.identity is not None
    assert (
        record.identity.realpath,
        record.identity.st_dev,
        record.identity.st_ino,
        record.identity.st_size,
        record.identity.st_mtime_ns,
    ) == expected


@pytest.mark.asyncio
async def test_binary_replacement_reprobes_once_and_unchanged_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch, b"agy-old")
    version = AsyncMock(side_effect=["1.1.16", "1.1.18"])
    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", version)

    first = await probe_and_publish_agy_support()
    assert first.installed_version == "1.1.16"
    await ensure_agy_support()
    assert version.await_count == 1

    _replace_agy(binary, b"agy-new-bytes")
    peeked = peek_agy_support()
    assert peeked.supported is False
    assert peeked.reason == AGY_REVALIDATING_REASON
    assert version.await_count == 1

    second = await ensure_agy_support()
    third = await ensure_agy_support()

    assert version.await_count == 2
    assert second.installed_version == "1.1.18"
    assert second.supported is True
    assert third == second
    assert peek_agy_support() == second


@pytest.mark.asyncio
async def test_concurrent_observers_share_one_reprobe_under_per_path_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch, b"agy-old")
    release = asyncio.Event()
    entered = asyncio.Event()
    in_flight = 0
    max_in_flight = 0
    calls = 0

    async def fake_version(provider: str, *, which: Any) -> str:
        nonlocal in_flight, max_in_flight, calls
        calls += 1
        if calls == 1:
            return "1.1.20"
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        entered.set()
        await release.wait()
        in_flight -= 1
        return "1.1.20"

    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", fake_version)
    first = await probe_and_publish_agy_support()
    assert first.installed_version == "1.1.20"
    _replace_agy(binary, b"agy-replaced")

    task1 = asyncio.create_task(ensure_agy_support())
    await entered.wait()
    others = [asyncio.create_task(ensure_agy_support()) for _ in range(4)]
    await _yield_loop()
    await _yield_loop()
    assert calls == 2  # startup + one shared re-probe
    assert max_in_flight == 1
    release.set()
    records = [await task1, *(await asyncio.gather(*others))]

    assert all(record == records[0] for record in records)
    assert all(record.installed_version == "1.1.20" for record in records)
    assert all(record.supported is True for record in records)
    assert calls == 2


@pytest.mark.asyncio
async def test_probe_executes_resolved_path_and_mismatch_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch, b"agy-pre")
    entered = asyncio.Event()
    proceed = asyncio.Event()
    seen_which: list[str] = []

    async def fake_version(provider: str, *, which: Any) -> str:
        seen_which.append(which(provider))
        entered.set()
        await proceed.wait()
        return "1.1.18"

    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", fake_version)

    task = asyncio.create_task(probe_and_publish_agy_support())
    await entered.wait()
    assert seen_which == [os.path.realpath(binary)]
    _replace_agy(binary, b"agy-mid-probe")
    proceed.set()
    await task

    assert agy_support_is_published() is False
    assert peek_agy_support().reason == AGY_UNPUBLISHED_REASON


@pytest.mark.asyncio
async def test_get_cli_version_invokes_resolved_executable_not_bare_name(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "agy-real"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    captured: list[tuple[str, ...]] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"1.1.18\n", b""

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return 0

    async def fake_exec(*args: str, **_kwargs: Any) -> _Proc:
        captured.append(tuple(args))
        return _Proc()

    with patch(
        "gobby.servers.provider_model_discovery.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        output = await get_cli_version("agy", which=lambda _name: str(binary))

    assert output == "1.1.18"
    assert captured == [(str(binary), "--version")]


@pytest.mark.asyncio
async def test_peek_never_awaits_or_subprocesses_on_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _install_agy(tmp_path, monkeypatch, b"agy-old")
    monkeypatch.setattr(
        "gobby.providers.version_gate.get_cli_version",
        AsyncMock(return_value="1.1.16"),
    )
    await probe_and_publish_agy_support()

    async def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("peek must not subprocess")

    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", boom)
    _replace_agy(binary, b"agy-new")

    peeked = peek_agy_support()
    assert peeked.supported is False
    assert peeked.reason == AGY_REVALIDATING_REASON
    assert not inspect.iscoroutinefunction(peek_agy_support)


@pytest.mark.asyncio
async def test_tool_chat_registry_sees_published_record_without_second_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_agy(tmp_path, monkeypatch)
    version = AsyncMock(return_value="1.1.18")
    monkeypatch.setattr("gobby.providers.version_gate.get_cli_version", version)
    await probe_and_publish_agy_support()

    service = build_daemon_tool_chat_service(DaemonConfig())
    web_chat = service.registry.binding(AICapability.WEB_CHAT, "agy")
    spawn = service.registry.binding(AICapability.AGENT_SPAWN, "agy")
    payload = _agy_snapshot_payload()

    assert web_chat is not None
    assert spawn is not None
    assert web_chat.available is False
    assert web_chat.reason == AGY_UNAVAILABLE_REASON
    assert web_chat.metadata["agy_supported"] is True
    assert web_chat.metadata["agy_installed_version"] == "1.1.18"
    assert spawn.metadata["agy_supported"] is True
    assert payload["support"]["supported"] is True
    assert payload["support"]["installed_version"] == "1.1.18"
    assert payload["refresh"]["sources"] == [{"source_key": "version_gate", "state": "ok"}]
    version.assert_awaited_once()


def test_lifespan_asserts_publication_and_does_not_probe() -> None:
    from gobby.servers._app_lifecycle import create_lifespan

    source = inspect.getsource(create_lifespan)
    assert "assert_agy_support_published" in source
    assert "probe_and_publish_agy_support" not in source


def test_run_gobby_awaits_probe_after_pid_ownership_before_runner() -> None:
    from gobby.runner import run_gobby

    source = inspect.getsource(run_gobby)
    assert source.index("claim_pid_file") < source.index("probe_and_publish_agy_support")
    assert source.index("probe_and_publish_agy_support") < source.index("GobbyRunner.create")
    assert "asyncio.run(" not in source


def test_get_cli_version_has_no_agy_caller_outside_version_gate() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "gobby"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "version_gate.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "get_cli_version" or not node.args:
                continue
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and arg0.value == "agy":
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


@pytest.mark.asyncio
async def test_execute_spawn_awaits_ensure_before_agy_result() -> None:
    from gobby.agents.spawn_executor import SpawnRequest, execute_spawn

    ensure = AsyncMock(
        return_value=AgySupportRecord(
            installed_version="1.1.18",
            required_version=AGY_REQUIRED_VERSION,
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
            identity=None,
        )
    )
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="agy",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    with patch("gobby.providers.version_gate.ensure_agy_support", ensure):
        result = await execute_spawn(request)
    ensure.assert_awaited_once()
    assert result.success is False
    assert AGY_UNAVAILABLE_REASON in (result.error or "")


@pytest.mark.asyncio
async def test_web_chat_create_session_awaits_ensure_before_agy_launch() -> None:
    from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

    ensure = AsyncMock(
        return_value=AgySupportRecord(
            installed_version="1.1.18",
            required_version=AGY_REQUIRED_VERSION,
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
            identity=None,
        )
    )
    manager = WebChatRuntimeManager(codex_client=None)
    with patch("gobby.providers.version_gate.ensure_agy_support", ensure):
        session = await manager.create_session(provider="agy", conversation_id="conv-agy")
    ensure.assert_awaited_once()
    from gobby.servers.websocket.chat.backends.agy import AgyManagedChatSession

    assert isinstance(session, AgyManagedChatSession)
    assert session.conversation_id == "conv-agy"


@pytest.mark.asyncio
async def test_capability_refresh_awaits_ensure_for_agy() -> None:
    from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator

    ensure = AsyncMock(return_value=peek_agy_support())
    collected = asyncio.Event()

    class _Collector:
        provider = "agy"
        sources: tuple[object, ...] = ()

        async def collect(self) -> object:
            collected.set()
            raise RuntimeError("collect")

    class _Store:
        def get_provider_snapshot(self, provider: str) -> None:
            return None

        def get_all_snapshots(self) -> tuple[object, ...]:
            return ()

        def replace_provider_snapshot(self, snapshot: object) -> None:
            return None

        def record_source_failure(self, provider: str, source_key: str, error: str) -> None:
            return None

    from gobby.providers.capabilities.collectors import CapabilityCollector
    from gobby.providers.capabilities.refresh import CapabilityStore

    collector = cast(CapabilityCollector, _Collector())
    with patch("gobby.providers.version_gate.ensure_agy_support", ensure):
        coordinator = CapabilityRefreshCoordinator(
            cast(CapabilityStore, _Store()),
            {"agy": collector},
        )
        await coordinator._refresh_provider(collector)

    ensure.assert_awaited_once()
    assert collected.is_set()


def test_side_effect_owners_use_peek_or_ensure() -> None:
    from gobby.agents.spawn_executor import execute_spawn
    from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator
    from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

    assert "ensure_agy_support" in inspect.getsource(execute_spawn)
    assert "ensure_agy_support" in inspect.getsource(CapabilityRefreshCoordinator._refresh_provider)
    assert "peek_agy_support" in inspect.getsource(WebChatRuntimeManager.health)
    assert "ensure_agy_support" in inspect.getsource(WebChatRuntimeManager.create_session)
