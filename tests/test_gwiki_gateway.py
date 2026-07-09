from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiReadSelectorError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


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
        self.stdout = stdout if stdout is not None else b'{"status": "ok"}'
        self.stderr = stderr
        self.timeout = timeout
        self.terminated = False
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.timeout:
            raise TimeoutError
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> None:
        self.waited = True


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
        "ask",
        "read",
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
    ):
        assert callable(getattr(gateway, method_name))
    assert not hasattr(gateway, "research")


async def test_ask_uses_read_only_cli_args(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "command": "ask",
        "query": "How do hooks work?",
        "status": "retrieved",
        "hits": [],
        "sources": [],
        "code_citations": [],
        "evidence": [],
        "prompt_token_budget": 12000,
        "prompt_tokens_estimated": 16,
        "warnings": [],
    }
    calls = _patch_subprocess(monkeypatch, [FakeProcess(stdout=_json_bytes(payload))])

    result = await GwikiGateway(
        binary="/bin/gwiki",
        project_root="/repo",
        timeout_seconds=1.0,
    ).ask("How do hooks work?", llm=True, ai="direct", require_ai=True, token_budget=2048)

    assert result["payload"] == payload
    assert calls == [
        (
            "/bin/gwiki",
            "ask",
            "How do hooks work?",
            "--llm",
            "--ai",
            "direct",
            "--require-ai",
            "--token-budget",
            "2048",
            "--project",
            "/repo",
            "--format",
            "json",
        )
    ]


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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ai": "direct"}, "ai require llm=True"),
        ({"require_ai": True}, "require_ai require llm=True"),
        ({"ai": "direct", "require_ai": True}, "ai and require_ai require llm=True"),
    ],
)
async def test_ask_rejects_ai_flags_without_llm(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await GwikiGateway(
            binary="/bin/gwiki",
            project_root="/repo",
            timeout_seconds=1.0,
        ).ask("How do hooks work?", **kwargs)


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


async def test_health_omits_gateway_scope_args(
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
    gateway = _gateway()

    result = await gateway.health()

    assert calls[0] == ("/bin/gwiki", "health", "--format", "json")
    assert result == {
        "ok": True,
        "command": "health",
        "payload": payload,
        "stderr": "",
    }
    assert report_path.read_text() == "# Wiki health report\n\nNo issues\n"


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


async def test_error_preserves_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
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
    process = FakeProcess(timeout=True)
    calls = _patch_subprocess(monkeypatch, [process])

    result = await GwikiGateway(binary="/bin/gwiki", timeout_seconds=0.01).health()

    assert calls == [("/bin/gwiki", "health", "--format", "json")]
    assert process.terminated is True
    assert process.killed is False
    assert process.waited is True
    assert result == {
        "ok": False,
        "command": "health",
        "status": "degraded",
        "payload": None,
        "stderr": "",
        "error": {
            "type": "timeout",
            "message": "gwiki command timed out",
        },
    }


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


async def test_index_serializes_watcher_and_cron_gateways_on_same_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        async def run_command(command_name: str, argv: Any) -> tuple[bytes, str]:
            entered.append(label)
            if gate is not None:
                first_entered.set()
                await gate.wait()
            return b'{"status": "ok"}', ""

        return run_command

    monkeypatch.setattr(cron_gateway, "_run_command", stub_run_command("cron", release_first))
    monkeypatch.setattr(watcher_gateway, "_run_command", stub_run_command("watcher", None))

    cron_index = asyncio.create_task(cron_gateway.index())
    await asyncio.wait_for(first_entered.wait(), timeout=2.0)

    watcher_index = asyncio.create_task(watcher_gateway.index())
    # One full loop turn: the watcher task runs as far as it can — with the
    # shared per-vault lock held it must park on acquire, not enter the stub.
    turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(turn.set)
    await turn.wait()

    assert entered == ["cron"]
    assert not watcher_index.done()

    release_first.set()
    await cron_index
    await watcher_index
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
        async def run_command(command_name: str, argv: Any) -> tuple[bytes, str]:
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
