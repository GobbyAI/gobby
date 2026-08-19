from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.gwiki_gateway import (
    GwikiCommandError,
    GwikiDaemonConfigUnavailableError,
    GwikiGateway,
    GwikiReadSelectorError,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FakeStream:
    def __init__(self, process: FakeProcess, payload: bytes) -> None:
        self._process = process
        self._payload = payload

    async def read(self) -> bytes:
        while self._process.timeout and not self._process.terminated and not self._process.killed:
            await asyncio.sleep(0.01)
        return self._payload


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes | None = None,
        stderr: bytes = b"",
        timeout: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout_payload = stdout if stdout is not None else b'{"status": "ok"}'
        self.stderr_payload = stderr
        self.stdout = FakeStream(self, self.stdout_payload)
        self.stderr = FakeStream(self, self.stderr_payload)
        self.timeout = timeout
        self.terminated = False
        self.killed = False
        self.waited = False
        self.communicate_input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.communicate_input = input
        if self.timeout:
            raise TimeoutError
        return self.stdout_payload, self.stderr_payload

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> None:
        self.waited = True
        while self.timeout and not self.terminated and not self.killed:
            await asyncio.sleep(0.01)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[FakeProcess],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args: str, **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        if not processes:
            raise AssertionError(f"no fake process available for command: {args!r}")
        return processes.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def _gateway() -> GwikiGateway:
    return GwikiGateway(binary="/bin/gwiki", project_root=Path("/repo"), topic="docs")


async def test_gateway_exposes_expected_methods() -> None:
    gateway = _gateway()

    for method_name in (
        "status",
        "index",
        "search",
        "read",
        "graph",
        "graph_artifacts",
        "pages",
        "export_pages",
        "backlinks",
        "ingest_file",
        "ingest_url",
        "collect",
        "compile",
        "audit",
        "trust",
        "health",
        "sources",
        "remove_source",
        "refresh",
        "sync_sessions",
        "write_page",
        "delete_page",
    ):
        assert callable(getattr(gateway, method_name))
    assert not hasattr(gateway, "research")


async def test_graph_builds_stdout_include_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"command": "graph", "graph": {"documents": [], "links": []}}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().graph(include="knowledge")

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "graph",
            "--stdout",
            "--include",
            "knowledge",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_agent_export_methods_build_file_writing_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(stdout=_json_bytes({"status": "completed"})),
            FakeProcess(stdout=_json_bytes({"status": "completed"})),
        ],
    )
    gateway = _gateway()

    await gateway.export_pages()
    await gateway.graph_artifacts()

    assert calls == [
        (
            "/bin/gwiki",
            "export",
            "pages",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
        (
            "/bin/gwiki",
            "graph",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
    ]


async def test_success_forwards_stderr_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"command": "graph", "graph": {"documents": [], "links": []}}
    _patch_subprocess(
        monkeypatch,
        [FakeProcess(stdout=_json_bytes(payload), stderr=b"gwiki diagnostic\n")],
    )

    result = await _gateway().graph(include="knowledge")

    assert result["payload"] == payload
    assert capsys.readouterr().err == "gwiki diagnostic\n"


async def test_pages_passes_optional_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"command": "pages", "pages": [], "outputs": []}
    calls = _patch_subprocess(
        monkeypatch,
        [FakeProcess(stdout=_json_bytes(payload)), FakeProcess(stdout=_json_bytes(payload))],
    )

    unfiltered = await _gateway().pages()
    filtered = await _gateway().pages(prefix="code/")

    assert unfiltered["payload"] == payload
    assert filtered["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "pages",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
        (
            "/bin/gwiki",
            "pages",
            "--prefix",
            "code/",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
    ]


async def test_write_page_builds_argv_and_threads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "command": "page-write",
        "path": "knowledge/notes/demo.md",
        "created": True,
        "bytes": 12,
        "content_hash": "abc123",
        "changed_paths": ["knowledge/notes/demo.md"],
    }
    process = FakeProcess(stdout=_json_bytes(payload))
    argv_calls: list[tuple[str, ...]] = []
    kwargs_calls: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> FakeProcess:
        argv_calls.append(args)
        kwargs_calls.append(kwargs)
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = await _gateway().write_page(
        path="knowledge/notes/demo.md",
        content="# Demo\nBody\n",
        expected_hash="deadbeef",
    )

    assert result["payload"] == payload
    assert argv_calls == [
        (
            "/bin/gwiki",
            "page",
            "write",
            "--path",
            "knowledge/notes/demo.md",
            "--mode",
            "upsert",
            "--expected-hash",
            "deadbeef",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]
    assert kwargs_calls[0]["stdin"] == asyncio.subprocess.PIPE
    assert process.communicate_input == b"# Demo\nBody\n"


async def test_write_page_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be one of create, upsert"):
        await _gateway().write_page(path="knowledge/a.md", content="x", mode="replace")


async def test_delete_page_builds_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "command": "page-delete",
        "path": "knowledge/notes/demo.md",
        "changed_paths": ["knowledge/notes/demo.md"],
    }
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().delete_page(path="knowledge/notes/demo.md")

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "page",
            "delete",
            "--path",
            "knowledge/notes/demo.md",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_gwiki_gateway_does_not_expose_ask() -> None:
    """Daemon-native GwikiGateway has no ask surface; wiki_ask stays deleted."""
    assert not hasattr(GwikiGateway, "ask")
    gateway = GwikiGateway(binary="/bin/gwiki", project_root="/repo", timeout_seconds=1.0)
    assert not hasattr(gateway, "ask")


async def test_search_passes_token_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"command": "search", "query": "hooks", "results": []}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().search("hooks", limit=5, token_budget=4096)

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "search",
            "hooks",
            "--limit",
            "5",
            "--token-budget",
            "4096",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_compile_builds_full_arg_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"command": "compile", "status": "written"}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().compile(
        "Hooks Overview",
        kind="topic",
        sources=["src-1", "src-2"],
        outline=["Intro", "Details"],
        target="knowledge/topics/hooks.md",
        write_intent=True,
        ai="direct",
    )

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "compile",
            "Hooks Overview",
            "--kind",
            "topic",
            "--source",
            "src-1",
            "--source",
            "src-2",
            "--outline",
            "Intro",
            "--outline",
            "Details",
            "--target",
            "knowledge/topics/hooks.md",
            "--write-intent",
            "--ai",
            "direct",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_compile_without_arguments_emits_bare_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"command": "compile", "status": "written"}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().compile()

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "compile",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_compile_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of concept, source, topic"):
        await _gateway().compile("Hooks Overview", kind="article")


async def test_sync_sessions_passes_archive_dir_and_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "sessions"
    payload = {"command": "sync-sessions", "status": "completed", "accepted": 2}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().sync_sessions(archive_dir=archive_dir, limit=10)

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "sync-sessions",
            "--archive-dir",
            str(archive_dir),
            "--limit",
            "10",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


async def test_resolve_binary_serializes_concurrent_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_resolve_native_bin(name: str) -> str:
        nonlocal calls
        calls += 1
        assert name == "gwiki"
        return "/bin/gwiki"

    monkeypatch.setattr("gobby.gwiki_gateway.resolve_native_bin", fake_resolve_native_bin)
    gateway = GwikiGateway()

    results = await asyncio.gather(*(gateway._resolve_binary() for _ in range(10)))

    assert results == ["/bin/gwiki"] * 10
    assert calls == 1


async def test_health_uses_gateway_scope_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "meta" / "health" / "latest.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("Wiki health report\n\nNo issues\n")
    payload = {
        "status": "healthy",
        "root": str(tmp_path),
        "text_path": "meta/health/latest.md",
    }
    calls = _patch_subprocess(
        monkeypatch,
        [FakeProcess(stdout=_json_bytes(payload))],
    )
    real_to_thread = asyncio.to_thread
    to_thread_calls: list[str] = []

    async def recording_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        to_thread_calls.append(func.__name__)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    gateway = _gateway()

    result = await gateway.health()

    assert calls[0] == (
        "/bin/gwiki",
        "health",
        "--project",
        "/repo",
        "--topic",
        "docs",
        "--format",
        "json",
    )
    assert result == {
        "ok": True,
        "command": "health",
        "payload": payload,
        "stderr": "",
    }
    assert to_thread_calls == ["_normalize_health_report_file"]
    assert report_path.read_text() == "# Wiki health report\n\nNo issues\n"


async def test_health_ignores_non_utf8_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "health.md"
    report_path.write_bytes(b"\xff\xfe")
    payload = {
        "status": "healthy",
        "root": str(tmp_path),
        "text_path": report_path.name,
    }
    _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().health()

    assert result["ok"] is True
    assert result["payload"] == payload
    assert report_path.read_bytes() == b"\xff\xfe"


async def test_health_ignores_report_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "health.md"
    report_path.write_text("Wiki health report\n", encoding="utf-8")
    payload = {
        "status": "healthy",
        "root": str(tmp_path),
        "text_path": report_path.name,
    }
    _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    def fail_write_text(
        path: Path,
        _data: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        assert path == report_path
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write_text)

    result = await _gateway().health()

    assert result["ok"] is True
    assert result["payload"] == payload


async def test_trust_uses_gateway_scope_args(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "command": "trust",
        "trust_status": {"status": "trusted"},
    }
    calls = _patch_subprocess(
        monkeypatch,
        [FakeProcess(stdout=_json_bytes(payload))],
    )
    gateway = _gateway()

    result = await gateway.trust()

    assert calls[0] == (
        "/bin/gwiki",
        "trust",
        "--project",
        "/repo",
        "--topic",
        "docs",
        "--format",
        "json",
    )
    assert result == {
        "ok": True,
        "command": "trust",
        "payload": payload,
        "stderr": "",
    }


async def test_error_preserves_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"status": "failed", "error": {"code": "bad_scope"}}
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(
                returncode=2,
                stdout=_json_bytes(payload),
                stderr=b"scope does not exist\n",
            )
        ],
    )

    with pytest.raises(GwikiCommandError) as exc_info:
        await _gateway().status()

    assert calls == [
        (
            "/bin/gwiki",
            "status",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]
    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == "scope does not exist"
    assert exc_info.value.payload == payload
    assert exc_info.value.to_envelope()["stderr"] == "scope does not exist"
    assert capsys.readouterr().err == ""


async def test_daemon_config_transport_is_unavailable_without_forwarding_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = b"Error: daemon effective config request failed: daemon could not be reached (timeout)"
    _patch_subprocess(
        monkeypatch,
        [FakeProcess(returncode=1, stderr=stderr)],
    )

    with pytest.raises(GwikiDaemonConfigUnavailableError) as exc_info:
        await _gateway().status()

    assert exc_info.value.command == "status"
    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr.endswith("(timeout)")
    assert capsys.readouterr().err == ""


async def test_prune_result_classifies_daemon_config_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = (
        b"Error: daemon effective config request failed: daemon could not be reached (unreachable)"
    )
    _patch_subprocess(
        monkeypatch,
        [FakeProcess(returncode=1, stderr=stderr)],
    )

    with pytest.raises(GwikiDaemonConfigUnavailableError) as exc_info:
        await _gateway().prune_all_scopes()

    assert exc_info.value.command == "prune"
    assert capsys.readouterr().err == ""


async def test_error_parses_structured_stderr_when_stdout_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "failed",
        "error": {
            "code": "bad_scope",
            "guidance": "Create the docs topic before querying status.",
        },
    }
    _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(
                returncode=2,
                stdout=b"",
                stderr=_json_bytes(payload),
            )
        ],
    )

    with pytest.raises(GwikiCommandError) as exc_info:
        await _gateway().status()

    assert exc_info.value.returncode == 2
    assert json.loads(exc_info.value.stderr) == payload
    assert exc_info.value.payload == payload
    assert exc_info.value.to_envelope()["payload"] == payload
    assert json.loads(exc_info.value.to_envelope()["stderr"]) == payload


async def test_timeout_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        stdout=b"partial output\n",
        stderr=b"still working\n",
        timeout=True,
    )
    calls = _patch_subprocess(monkeypatch, [process])

    result = await GwikiGateway(
        binary="/bin/gwiki",
        project_root="/repo",
        topic="docs",
        timeout_seconds=0.01,
    ).health()

    assert calls == [
        (
            "/bin/gwiki",
            "health",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]
    assert process.terminated is True
    assert process.killed is False
    assert process.waited is True
    assert result["ok"] is False
    assert result["command"] == "health"
    assert result["status"] == "degraded"
    assert result["payload"] is None
    assert result["stdout"] == "partial output"
    assert result["stderr"] == "still working"
    assert result["scope"] == {"project_root": "/repo", "topic": "docs"}
    assert result["error"]["type"] == "timeout"
    assert result["error"]["message"] == "gwiki command timed out"
    assert result["error"]["timeout_seconds"] == 0.01
    assert result["error"]["elapsed_seconds"] >= 0


async def test_timeout_does_not_wait_for_process_wait_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellationResistantProcess(FakeProcess):
        async def wait(self) -> None:
            self.waited = True
            while self.timeout and not self.terminated and not self.killed:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue

    process = CancellationResistantProcess(timeout=True)
    _patch_subprocess(monkeypatch, [process])
    gateway = GwikiGateway(
        binary="/bin/gwiki",
        project_root="/repo",
        timeout_seconds=0.01,
    )
    command = asyncio.create_task(gateway.health())

    try:
        await asyncio.sleep(0.05)
        assert command.done()
        result = await command
    finally:
        process.terminate()
        await command

    assert result["status"] == "degraded"
    assert result["error"]["type"] == "timeout"


async def test_cancellation_terminates_and_reaps_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(timeout=True)
    wait_started = asyncio.Event()
    original_wait = process.wait

    async def signaling_wait() -> None:
        wait_started.set()
        await original_wait()

    process.wait = signaling_wait
    _patch_subprocess(monkeypatch, [process])
    task = asyncio.create_task(_gateway().health())
    await asyncio.wait_for(wait_started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is False
    assert process.waited is True


async def test_process_cleanup_escalates_to_kill() -> None:
    class StubbornProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__(timeout=True)
            self.wait_count = 0

        async def wait(self) -> None:
            self.waited = True
            self.wait_count += 1
            if self.wait_count == 1:
                raise TimeoutError

    process = StubbornProcess()

    await _gateway()._kill_process(process)

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_count == 2


async def test_read_status_payloads_are_not_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(stdout=_json_bytes({"status": "not_found", "path": "missing.md"})),
            FakeProcess(stdout=_json_bytes({"status": "invalid_request", "error": "bad selector"})),
            FakeProcess(stdout=_json_bytes({"status": "ambiguous", "matches": ["A", "B"]})),
        ],
    )
    gateway = _gateway()

    not_found = await gateway.read(path="missing.md")
    invalid = await gateway.read(title="Bad")
    ambiguous = await gateway.read(title="Ambiguous")

    assert calls == [
        (
            "/bin/gwiki",
            "read",
            "--path",
            "missing.md",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
        (
            "/bin/gwiki",
            "read",
            "--title",
            "Bad",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
        (
            "/bin/gwiki",
            "read",
            "--title",
            "Ambiguous",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
    ]
    assert not_found["payload"]["status"] == "not_found"
    assert invalid["payload"]["status"] == "invalid_request"
    assert ambiguous["payload"]["status"] == "ambiguous"

    with pytest.raises(GwikiReadSelectorError):
        await gateway.read(path="one.md", title="One")
    with pytest.raises(GwikiReadSelectorError):
        await gateway.read()
    with pytest.raises(GwikiReadSelectorError):
        await gateway.read(path="")


async def test_sources_preserves_cli_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "scope": {"project": "/repo", "topic": "docs"},
        "sources": [{"id": "src_1", "kind": "url", "url": "https://example.test/a"}],
    }
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().sources()

    assert calls == [
        (
            "/bin/gwiki",
            "sources",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]
    assert result == {"ok": True, "command": "sources", "payload": payload, "stderr": ""}


async def test_remove_source_preserves_cli_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    dry_run_payload = {
        "status": "dry_run",
        "source": {"id": "src_1"},
        "index_status": {"indexed": 3},
    }
    removed_payload = {
        "status": "removed",
        "removed": {"source_id": "src_1", "asset_deleted": False},
        "index_status": {"indexed": 2},
    }
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(stdout=_json_bytes(dry_run_payload), stderr=b"preview only\n"),
            FakeProcess(stdout=_json_bytes(removed_payload), stderr=b"removed source\n"),
        ],
    )
    gateway = _gateway()

    dry_run = await gateway.remove_source("src_1", dry_run=True, yes=False, keep_asset=False)
    removed = await gateway.remove_source("src_1", dry_run=False, yes=True, keep_asset=True)

    assert calls == [
        (
            "/bin/gwiki",
            "remove-source",
            "--id",
            "src_1",
            "--dry-run",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
        (
            "/bin/gwiki",
            "remove-source",
            "--id",
            "src_1",
            "--yes",
            "--keep-asset",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        ),
    ]
    assert dry_run["payload"] == dry_run_payload
    assert dry_run["stderr"] == "preview only"
    assert removed["payload"] == removed_payload
    assert removed["stderr"] == "removed source"


async def test_ingest_url_passes_batch_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "partial",
        "scope": {"project": "/repo", "topic": "docs"},
        "accepted": [{"url": "https://example.test/a"}],
        "failed": [{"url": "https://example.test/b", "error": "404"}],
        "indexed": {"count": 1},
    }
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await _gateway().ingest_url(["https://example.test/a", "https://example.test/b"])

    assert calls == [
        (
            "/bin/gwiki",
            "ingest-url",
            "https://example.test/a",
            "https://example.test/b",
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]
    assert result["payload"] == payload


@pytest.mark.parametrize("max_age_hours", [0, 24, 8760])
async def test_ingest_url_passes_max_age_hours(
    monkeypatch: pytest.MonkeyPatch,
    max_age_hours: int,
) -> None:
    payload = {"status": "success", "accepted": [], "failed": [], "cached": []}
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    await _gateway().ingest_url(
        ["https://example.test/a"],
        max_age_hours=max_age_hours,
    )

    assert calls == [
        (
            "/bin/gwiki",
            "ingest-url",
            "https://example.test/a",
            "--max-age-hours",
            str(max_age_hours),
            "--project",
            "/repo",
            "--topic",
            "docs",
            "--format",
            "json",
        )
    ]


@pytest.mark.parametrize("max_age_hours", [-1, 8761])
async def test_ingest_url_rejects_invalid_max_age_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    max_age_hours: int,
) -> None:
    calls = _patch_subprocess(monkeypatch, [])

    with pytest.raises(ValueError, match="max_age_hours must be between 0 and 8760"):
        await _gateway().ingest_url(
            ["https://example.test/a"],
            max_age_hours=max_age_hours,
        )

    assert calls == []


async def test_ingest_url_preserves_partial_and_all_failed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_failed_payload = {
        "status": "failed",
        "accepted": [],
        "failed": [{"url": "https://example.test/a", "error": "timeout"}],
    }
    _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(
                returncode=3,
                stdout=_json_bytes(all_failed_payload),
                stderr=b"all urls failed\n",
            )
        ],
    )

    with pytest.raises(GwikiCommandError) as exc_info:
        await _gateway().ingest_url(["https://example.test/a"])

    assert exc_info.value.payload == all_failed_payload
    assert exc_info.value.stderr == "all urls failed"
    assert exc_info.value.to_envelope()["payload"] == all_failed_payload


async def test_refresh_uses_gateway_scope_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "dry_run",
        "scope": "topic",
        "refreshed": [{"id": "src_1"}],
        "unchanged": [{"id": "src_2"}],
        "failed": [{"id": "src_3", "error": "gone"}],
        "indexed": {"count": 1},
        "index_status": {"queued": 0},
    }
    failed_payload = {
        "status": "failed",
        "scope": "topic",
        "refreshed": [],
        "unchanged": [],
        "failed": [{"id": "src_3", "error": "gone"}],
        "indexed": {"count": 0},
        "index_status": {"queued": 0},
    }
    calls = _patch_subprocess(
        monkeypatch,
        [
            FakeProcess(stdout=_json_bytes(payload), stderr=b"dry run\n"),
            FakeProcess(returncode=4, stdout=_json_bytes(failed_payload), stderr=b"all failed\n"),
        ],
    )
    gateway = _gateway()

    result = await gateway.refresh(source_ids=["src_1", "src_2"], dry_run=True)

    assert calls[0] == (
        "/bin/gwiki",
        "refresh",
        "--id",
        "src_1",
        "--id",
        "src_2",
        "--dry-run",
        "--project",
        "/repo",
        "--topic",
        "docs",
        "--format",
        "json",
    )
    assert result == {
        "ok": True,
        "command": "refresh",
        "payload": payload,
        "stderr": "dry run",
    }

    with pytest.raises(GwikiCommandError) as exc_info:
        await gateway.refresh(source_ids=["src_3"])

    assert exc_info.value.payload == failed_payload
    assert exc_info.value.stderr == "all failed"


def _make_vault(repo: Path) -> Path:
    vault = repo / "wiki"
    state = vault / "_gwiki"
    state.mkdir(parents=True)
    (state / "scope.json").write_text("{}")
    return vault


async def test_vault_lock_key_unifies_repo_root_and_vault_dir(tmp_path: Path) -> None:
    """Cron passes the repo root, the watcher passes the vault dir — same lock."""
    repo = tmp_path / "repo"
    vault = _make_vault(repo)
    other_repo = tmp_path / "other"
    _make_vault(other_repo)

    cron_style = GwikiGateway(binary="/bin/gwiki", project_root=repo)
    watcher_style = GwikiGateway(binary="/bin/gwiki", project_root=vault)
    other = GwikiGateway(binary="/bin/gwiki", project_root=other_repo)

    assert await cron_style._vault_lock_key() == await watcher_style._vault_lock_key()
    assert await cron_style._vault_lock_key() != await other._vault_lock_key()
    assert (
        await GwikiGateway(binary="/bin/gwiki", topic="research")._vault_lock_key()
        == await GwikiGateway(
            binary="/bin/gwiki", project_root=repo, topic="research"
        )._vault_lock_key()
    )


@pytest.mark.parametrize("method_name", ["index", "export_pages", "graph_artifacts"])
async def test_write_commands_serialize_gateways_on_same_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    repo = tmp_path / "repo"
    vault = _make_vault(repo)
    cron_gateway = GwikiGateway(binary="/bin/gwiki", project_root=repo)
    watcher_gateway = GwikiGateway(binary="/bin/gwiki", project_root=vault)

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    entered: list[str] = []

    def stub_run_command(
        label: str,
        gate: asyncio.Event | None,
    ) -> Any:
        async def run_command(
            command_name: str,
            argv: Any,
            *,
            stdin_data: bytes | None = None,
        ) -> tuple[bytes, str]:
            entered.append(label)
            if gate is not None:
                first_entered.set()
                await gate.wait()
            return b'{"status": "ok"}', ""

        return run_command

    monkeypatch.setattr(cron_gateway, "_run_command", stub_run_command("cron", release_first))
    monkeypatch.setattr(watcher_gateway, "_run_command", stub_run_command("watcher", None))

    cron_command = asyncio.create_task(getattr(cron_gateway, method_name)())
    await asyncio.wait_for(first_entered.wait(), timeout=2.0)

    watcher_command = asyncio.create_task(getattr(watcher_gateway, method_name)())
    # One full loop turn: the watcher task runs as far as it can — with the
    # shared per-vault lock held it must park on acquire, not enter the stub.
    turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(turn.set)
    await turn.wait()

    assert entered == ["cron"]
    assert not watcher_command.done()

    release_first.set()
    await cron_command
    await watcher_command
    assert entered == ["cron", "watcher"]


async def test_index_runs_concurrently_across_different_vaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _make_vault(repo_a)
    _make_vault(repo_b)
    gateway_a = GwikiGateway(binary="/bin/gwiki", project_root=repo_a)
    gateway_b = GwikiGateway(binary="/bin/gwiki", project_root=repo_b)

    a_entered = asyncio.Event()
    b_entered = asyncio.Event()

    def stub_run_command(entered_event: asyncio.Event, other_event: asyncio.Event) -> Any:
        async def run_command(
            command_name: str,
            argv: Any,
            *,
            stdin_data: bytes | None = None,
        ) -> tuple[bytes, str]:
            entered_event.set()
            # Both stubs must be inside their subprocess call at once; a
            # wrongly shared lock would leave one of these waits unsatisfied.
            await asyncio.wait_for(other_event.wait(), timeout=2.0)
            return b'{"status": "ok"}', ""

        return run_command

    monkeypatch.setattr(gateway_a, "_run_command", stub_run_command(a_entered, b_entered))
    monkeypatch.setattr(gateway_b, "_run_command", stub_run_command(b_entered, a_entered))

    result_a, result_b = await asyncio.wait_for(
        asyncio.gather(gateway_a.index(), gateway_b.index()),
        timeout=4.0,
    )

    assert a_entered.is_set() and b_entered.is_set()
    assert result_a["ok"] is True
    assert result_b["ok"] is True


async def test_mutating_gateway_inherits_held_singleton_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.runner_pid_file import INHERITED_LOCK_FD_ENV, claim_pid_file

    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    claim = claim_pid_file(home / "gobby.pid", role="daemon")
    assert claim is not None
    lock_fd = claim.fileno()
    captured: list[dict[str, object]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> FakeProcess:
        captured.append({"args": args, **kwargs})
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    try:
        gateway = GwikiGateway(binary="/bin/gwiki", project_root=tmp_path / "repo")
        await gateway.index()
    finally:
        claim.release()

    assert captured
    env = captured[0]["env"]
    assert isinstance(env, dict)
    assert env[INHERITED_LOCK_FD_ENV] == str(lock_fd)
    assert captured[0].get("pass_fds") == (lock_fd,)
