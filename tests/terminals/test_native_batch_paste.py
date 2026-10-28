"""Long native batch input is a paste; submission remains a separate key."""

import asyncio
import base64
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import pytest

from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead
from gobby.terminals.host_client import (
    HostBatchOperation,
    HostBatchTarget,
    HostClient,
    HostCommandError,
)
from gobby.terminals.native_runtime import (
    NativeBatchOperation,
    NativeBatchTarget,
    NativeTerminalRuntime,
)
from gobby.terminals.pane_io import RuntimePaneIO
from gobby.terminals.runtime import Delivered
from tests.terminals.test_host_client import _Writer
from tests.terminals.test_native_runtime import FakeHostClient, _native_terminal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("via_pane", [False, True], ids=["batch", "runtime-pane"])
@pytest.mark.asyncio
async def test_long_native_batch_text_is_one_paste_with_separate_enter(via_pane: bool) -> None:
    host = FakeHostClient()
    ledger = ComposerLedger()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch, composer_ledger=ledger)
    terminal = _native_terminal(host, host_terminal_id="ht-1")
    ledger.release(terminal.id)
    text = "HEAD:" + "界" * 2001 + ":TAIL"
    if via_pane:
        pane = RuntimePaneIO(runtime, terminal)
        assert (await pane.type_text(text))[0] is True
        assert (await pane.send_key("enter"))[0] is True
    else:
        results = await runtime.write_batch(
            [
                NativeBatchTarget(
                    "seat",
                    terminal,
                    (
                        NativeBatchOperation("text", text),
                        NativeBatchOperation("key", "enter", delay_ms=15),
                    ),
                )
            ]
        )
        assert isinstance(results[0].outcome, Delivered)
    operations = [op for batch in host.batches for target in batch for op in target.operations]
    assert [op.kind for op in operations] == ["paste", "key"], (
        "preserve the entire long prompt in one paste and send Return separately"
    )
    assert operations[0].data == text.encode("utf-8")
    assert operations[1].data == b"\r"
    assert ledger.read(terminal.id) == LedgerRead("held", pending=text)
    ledger.record_submit(terminal.id)
    assert ledger.read(terminal.id) == LedgerRead("empty")


@pytest.mark.asyncio
async def test_batch_text_carriage_return_retains_ledger_submit_flag() -> None:
    host = FakeHostClient()
    ledger = ComposerLedger()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch, composer_ledger=ledger)
    terminal = _native_terminal(host, host_terminal_id="ht-1")
    ledger.release(terminal.id)
    text = "x" * 2001 + "\r"
    results = await runtime.write_batch(
        [NativeBatchTarget("seat", terminal, (NativeBatchOperation("text", text),))]
    )
    assert isinstance(results[0].outcome, Delivered)
    assert ledger.read(terminal.id) == LedgerRead("held", pending=text)
    ledger.record_submit(terminal.id)
    assert ledger.read(terminal.id) == LedgerRead("empty"), "the submit flag survives paste routing"


@pytest.mark.asyncio
async def test_host_batch_client_encodes_paste_without_losing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HostClient(asyncio.StreamReader(), _Writer())
    result: dict[str, Any] = {
        "recipient_id": "seat",
        "host_terminal_id": "ht-1",
        "ok": True,
        "written": True,
    }
    roundtrip = AsyncMock(return_value={"results": [result]})
    monkeypatch.setattr(client, "_mutating_roundtrip", roundtrip)
    text = ("HEAD:" + "界" * 2001 + ":TAIL").encode("utf-8")
    # Exercise the new wire kind against the old Literal before implementation.
    paste = HostBatchOperation(cast(Literal["text", "key"], "paste"), text, delay_ms=15)
    try:
        results = await client.write_batch(
            [HostBatchTarget("seat", "ht-1", (paste, HostBatchOperation("key", b"\r")))]
        )
    except HostCommandError as exc:
        pytest.fail(f"host batch protocol must carry paste: {exc}")
    finally:
        await client.close()
    assert results == [result]
    request = roundtrip.await_args
    assert request is not None
    operations = request.args[0]["targets"][0]["operations"]
    assert [op["kind"] for op in operations] == ["paste", "key"]
    assert [base64.b64decode(op["data"]) for op in operations] == [text, b"\r"]
    assert operations[0]["delay_ms"] == 15
