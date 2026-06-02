from __future__ import annotations

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


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[FakeProcess],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args: str, **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def _gateway() -> GwikiGateway:
    return GwikiGateway(binary="/bin/gwiki", project=Path("/repo"), topic="docs")


async def test_gateway_exposes_expected_methods() -> None:
    gateway = _gateway()

    for method_name in (
        "status",
        "index",
        "search",
        "read",
        "backlinks",
        "ingest_file",
        "ingest_url",
        "collect",
        "research",
        "compile",
        "audit",
        "health",
        "sources",
        "remove_source",
        "refresh",
    ):
        assert callable(getattr(gateway, method_name))


async def test_health_omits_gateway_scope_args(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "healthy"}
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

    stderr_text = (
        '{"status": "failed", "error": {"code": "bad_scope", '
        '"guidance": "Create the docs topic before querying status."}}'
    )
    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == stderr_text
    assert exc_info.value.payload == payload
    assert exc_info.value.to_envelope()["payload"] == payload
    assert exc_info.value.to_envelope()["stderr"] == stderr_text


async def test_timeout_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(timeout=True)
    calls = _patch_subprocess(monkeypatch, [process])

    result = await GwikiGateway(binary="/bin/gwiki", timeout_seconds=0.01).health()

    assert calls == [("/bin/gwiki", "health", "--format", "json")]
    assert process.killed is True
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


async def test_refresh_passes_scope_and_preserves_payload(
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

    result = await gateway.refresh(scope="topic", source_ids=["src_1", "src_2"], dry_run=True)

    assert calls[0] == (
        "/bin/gwiki",
        "refresh",
        "--scope",
        "topic",
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
        await gateway.refresh(scope="topic", source_ids=["src_3"])

    assert exc_info.value.payload == failed_payload
    assert exc_info.value.stderr == "all failed"
