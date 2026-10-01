"""Safety and ordering of test-only real-writer instrumentation; no live processes."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import Delivered, WriteOutcome
from gobby.terminals.write_coordinator import WriteRequest
from tests.e2e.composer_proof import (
    ProofRefused,
    ProofScope,
    Surface,
    require_private_root,
    sealed_environment,
)
from tests.e2e.composer_proof_trace import WAKE_TEXT, ProofTrace, exchange_trace, observe_dispatch

pytestmark = pytest.mark.unit


def test_private_root_refuses_shared_or_symlinked_state(tmp_path: Path) -> None:
    root = tmp_path / "proof"
    root.mkdir(mode=0o700)
    assert require_private_root(root) == root.resolve()
    root.chmod(0o755)
    with pytest.raises(ProofRefused, match="private"):
        require_private_root(root)
    root.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ProofRefused, match="symlink"):
        require_private_root(link)


def test_sealed_environment_excludes_unanticipated_credentials_and_identity(tmp_path: Path) -> None:
    environment = sealed_environment(
        {
            "NEW_VENDOR_SECRET": "fake-secret",
            "GOBBY_SESSION_ID": "foreign",
            "TERM": "xterm-256color",
        },
        home=tmp_path,
        native_bin=tmp_path / "bin",
    )
    assert "NEW_VENDOR_SECRET" not in environment and "GOBBY_SESSION_ID" not in environment
    assert environment["HOME"] == str(tmp_path)
    assert environment["PATH"].split(":")[0] == str(tmp_path / "bin")
    assert environment["GOBBY_TEST_PROTECT"] == "1"


@pytest.mark.asyncio
async def test_receipt_and_barrier_follow_actual_dispatch_and_never_contain_payload() -> None:
    staged = asyncio.Event()
    release = asyncio.Event()
    events: list[dict[str, object]] = []
    calls: list[str] = []
    request = WriteRequest("proof-terminal", "proof-action", "automatic", "text", "fake-secret")

    async def dispatch(write: WriteRequest, row: Terminal | None) -> WriteOutcome:
        calls.append(write.payload)
        return Delivered()

    async def exchange(event: dict[str, object]) -> None:
        events.append(event)
        if event["phase"] == "after":
            assert calls == ["fake-secret"]
            staged.set()
            await release.wait()

    task = asyncio.create_task(observe_dispatch(request, None, dispatch, exchange))
    try:
        await asyncio.wait_for(staged.wait(), 1)
        assert not task.done()
        assert events[-1]["outcome"] == "Delivered"
        assert events[-1]["payload_sha256"] == hashlib.sha256(b"fake-secret").hexdigest()
        assert "fake-secret" not in repr(events)
        release.set()
        assert isinstance(await asyncio.wait_for(task, 1), Delivered)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_refused_scope_dispatches_zero_bytes() -> None:
    calls: list[str] = []
    request = WriteRequest("foreign-terminal", "proof-action", "automatic", "text", "hello")

    async def dispatch(write: WriteRequest, row: Terminal | None) -> WriteOutcome:
        calls.append(write.payload)
        return Delivered()

    async def refuse(event: dict[str, object]) -> None:
        raise ProofRefused("foreign binding")

    with pytest.raises(ProofRefused, match="foreign binding"):
        await observe_dispatch(request, None, dispatch, refuse)
    assert calls == []


def registered_trace(tmp_path: Path) -> tuple[ProofTrace, dict[str, object]]:
    own = Surface("claude", str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    scope = ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40)
    trace = ProofTrace(scope, tmp_path / "trace.sock")
    trace.register(own)
    event: dict[str, object] = {
        "terminal_id": own.terminal_id,
        "session_id": own.session_id,
        "project_id": own.project_id,
        "host_epoch": own.host_epoch,
        "origin": "automatic",
        "kind": "text",
        "submit": False,
        "payload_sha256": hashlib.sha256(WAKE_TEXT.encode()).hexdigest(),
        "action_sha256": "b" * 64,
        "phase": "after",
        "outcome": "Delivered",
    }
    return trace, event


@pytest.mark.asyncio
async def test_trace_refuses_extra_raw_fields_and_invalid_phases(tmp_path: Path) -> None:
    trace, event = registered_trace(tmp_path)
    with pytest.raises(ProofRefused, match="event"):
        await trace.accept({**event, "raw_secret": "fake-secret"})
    with pytest.raises(ProofRefused, match="phase"):
        await trace.accept({**event, "phase": "imagined"})
    assert trace.events == []


@pytest.mark.asyncio
async def test_socket_barrier_holds_the_real_after_receipt_and_cleanup_releases_it(
    tmp_path: Path,
) -> None:
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    trace.arm(own, WAKE_TEXT)
    # macOS AF_UNIX paths cannot fit pytest's deeply nested temporary directory.
    socket_root = tempfile.TemporaryDirectory(prefix="proof-", dir="/tmp")
    trace.socket = Path(socket_root.name) / "trace.sock"
    await trace.start()
    task = asyncio.create_task(exchange_trace(trace.socket, event))
    try:
        await asyncio.wait_for(trace.staged.wait(), 1)
        assert not task.done()
        assert trace.events[0]["sequence"] == 1
        trace.release.set()
        await asyncio.wait_for(task, 1)
        with pytest.raises(ProofRefused, match="binding"):
            await trace.accept({**event, "session_id": str(uuid4())})
        assert len(trace.events) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await trace.close()
        socket_root.cleanup()
    assert not trace.socket.exists() and trace.handlers == set()


@pytest.mark.asyncio
async def test_retry_attempt_receipts_carry_real_outcome_without_message_content(
    tmp_path: Path,
) -> None:
    trace, write = registered_trace(tmp_path)
    event = {key: write[key] for key in ("terminal_id", "session_id", "project_id", "host_epoch")}
    event.update(
        phase="wake",
        requested_session_id=write["session_id"],
        delivered=False,
        skipped="composer_occupied",
        priority="urgent",
        monotonic=15.0,
    )
    await trace.accept(event)
    assert trace.events[0]["skipped"] == "composer_occupied"
    assert trace.events[0]["requested_session_id"] == write["session_id"]
    with pytest.raises(ProofRefused, match="binding"):
        await trace.accept({**event, "host_epoch": "foreign-epoch"})
    assert len(trace.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_command_gate_matches_actual_newline_transport(tmp_path: Path, command: str) -> None:
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    trace.arm(own, command)
    event["payload_sha256"] = hashlib.sha256((command + "\n").encode()).hexdigest()
    task = asyncio.create_task(trace.accept(event))
    try:
        await asyncio.wait_for(trace.staged.wait(), 0.1)
        assert not task.done(), "real command write must remain held before Enter"
        trace.release.set()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
