from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI

from gobby.hooks import grok_pending_context, inbox, inbox_envelopes
from gobby.hooks.envelope_dedupe import get_processed_envelope_dir, mark_envelope_processed
from gobby.hooks.replay_decision import ReplayDecision
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.storage import hook_receipts, workspace_machine_scope
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from tests.fixtures.postgres import TEST_USER_ID


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["unlink", "requeued"])
async def test_replay_cleanup_preserves_replaced_delivery_receipt(
    tmp_path: Path,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    disposition: Literal["unlink", "requeued"],
) -> None:
    machine_id = "37e78430-9ce8-447b-8a68-0688cde4a884"
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    session_id = session_manager.register_session(
        external_id="receipt-replacement-session",
        machine_id=machine_id,
        source="grok",
        project_id=sample_project["id"],
    )
    envelope_id = "n-0000000000001-original"
    receipt = hook_receipts.prepare_receipt(
        session_manager.db, session_id=session_id, envelope_id=envelope_id
    )
    path = tmp_path / f"{envelope_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hook_type": "pre-tool-use",
                "source": "grok",
                "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
                "input_data": {"session_id": session_id},
            }
        ),
        encoding="utf-8",
    )
    ack = {
        "schema_version": 1,
        "kind": "delivery-receipt",
        "receipt_id": receipt.receipt_id,
        "original_envelope_id": envelope_id,
        "delivery_generation": receipt.delivery_generation,
    }

    def settle_during_decision(
        app: Any, current_id: str | None, envelope: dict[str, Any], **kwargs: Any
    ) -> ReplayDecision:
        assert current_id == envelope_id
        assert envelope["hook_type"] == "pre-tool-use"
        replacement = tmp_path / "replacement.tmp"
        replacement.write_text(json.dumps(ack), encoding="utf-8")
        replacement.replace(path)  # ghook's atomic envelope -> receipt replacement.
        return ReplayDecision(disposition)

    monkeypatch.setattr(inbox, "decide_replay", settle_during_decision)
    app = FastAPI()
    app.state.database = session_manager.db

    assert await inbox.drain_hook_inbox_once(app, inbox_dir=tmp_path, include_fresh=True) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == ack
    assert inbox.consume_pending_delivery_receipts(app, inbox_dir=tmp_path) == 1
    assert not path.exists()
    later = utc_now() + timedelta(minutes=1)
    monkeypatch.setattr(hook_receipts, "utc_now", lambda: later)
    assert (
        hook_receipts.release_and_reprepare_for_session(
            session_manager.db, session_id=session_id, envelope_id="next-hook"
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize("disposition", ["retain", "unlink", "archived", "below_floor"])
@pytest.mark.asyncio
async def test_replay_disposition_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: Literal["retain", "unlink", "archived", "below_floor"],
) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text(json.dumps({"schema_version": 1, "hook_type": "stop", "source": "codex"}))
    replacement = {"kind": "delivery-receipt", "receipt_id": "replacement"}

    def decide(*args: Any, **kwargs: Any) -> ReplayDecision:
        path.write_text(json.dumps(replacement))
        return ReplayDecision(disposition)

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", decide)
    await inbox.drain_hook_inbox_once(FastAPI(), tmp_path, include_fresh=True)
    assert json.loads(path.read_text()) == replacement
    if disposition in {"archived", "below_floor"}:
        quarantine = inbox_envelopes.get_hook_quarantine_dir(tmp_path)
        assert not quarantine.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retained_envelope_stays_public_during_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "n-1-retained.json"
    path.write_text(json.dumps({"schema_version": 1, "hook_type": "stop", "source": "grok"}))

    def retain(*args: Any, **kwargs: Any) -> ReplayDecision:
        assert path.exists(), "Missing public path would falsely signal delivery to Grok"
        return ReplayDecision("retain")

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", retain)
    assert await inbox.drain_hook_inbox_once(FastAPI(), tmp_path, include_fresh=True) == 0
    assert path.exists()


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 401, 403, 409, 503])
@pytest.mark.asyncio
async def test_replay_response_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text(json.dumps({"schema_version": 1, "hook_type": "stop", "source": "codex"}))
    replacement = {"kind": "delivery-receipt", "receipt_id": "replacement"}

    async def post(*args: Any, **kwargs: Any) -> httpx.Response:
        path.write_text(json.dumps(replacement))
        return httpx.Response(status)

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", lambda *a, **kw: ReplayDecision("execute"))
    monkeypatch.setattr(inbox, "_post_envelope", post)
    await inbox.drain_hook_inbox_once(FastAPI(), tmp_path, include_fresh=True)
    assert json.loads(path.read_text()) == replacement


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["retain", "requeued"])
async def test_grok_ack_decision_keeps_public_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    path = tmp_path / "n-1-original.json"
    original = {"schema_version": 1, "hook_type": "pre-tool-use", "source": "grok"}
    path.write_text(json.dumps(original))
    replacement = {"kind": "delivery-receipt", "receipt_id": "replacement"}

    async def post(*args: Any, **kwargs: Any) -> httpx.Response:
        assert json.loads(path.read_text()) == original
        assert not list(tmp_path.glob("*.claimed.tmp"))
        return httpx.Response(200)

    def pending(*args: Any, **kwargs: Any) -> str:
        assert json.loads(path.read_text()) == original
        assert not list(tmp_path.glob("*.claimed.tmp"))
        if decision == "requeued":
            path.write_text(json.dumps(replacement))
        return decision

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", lambda *a, **kw: ReplayDecision("execute"))
    monkeypatch.setattr(inbox, "_post_envelope", post)
    monkeypatch.setattr(grok_pending_context, "decide_pending_inbox_envelope", pending)
    app = FastAPI()
    app.state.hook_manager = object()
    assert await inbox.drain_hook_inbox_once(app, tmp_path, include_fresh=True) == 1
    assert json.loads(path.read_text()) == (replacement if decision == "requeued" else original)
    assert not list(tmp_path.glob("*.claimed.tmp"))


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["unlink", "quarantine", "invalid-json"])
def test_replacement_at_claim_rename_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text("loaded envelope")
    signature = inbox_envelopes.file_signature(path)
    assert signature is not None
    rename = Path.rename

    def replace_before_rename(source: Path, target: Path) -> Path:
        replacement = tmp_path / "receipt.tmp"
        replacement.write_text("new receipt")
        replacement.replace(source)
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", replace_before_rename)
    if operation == "unlink":
        inbox_envelopes.unlink_envelope(path, signature)
    elif operation == "quarantine":
        assert not inbox_envelopes.quarantine_file(
            path, reason="superseded_hook", detail="test", expected_signature=signature
        )
    else:
        assert inbox_envelopes.load_envelope(path) is None
    assert path.read_text() == "new receipt"
    assert not list(tmp_path.glob("*.claimed.tmp"))
    assert not inbox_envelopes.get_hook_quarantine_dir(tmp_path).exists()


@pytest.mark.unit
def test_receipt_sweep_cannot_replace_active_drain_claim(tmp_path: Path) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text("original hook")
    signature = inbox_envelopes.file_signature(path)
    assert signature is not None
    with inbox_envelopes.claim_inbox_file(path, signature) as claimed:
        assert claimed is not None
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "delivery-receipt",
                    "receipt_id": "receipt",
                    "delivery_generation": 1,
                }
            )
        )
        assert inbox.consume_pending_delivery_receipts(FastAPI(), tmp_path) == 1
        assert claimed.read_text() == "original hook"
        assert inbox._restore_orphaned_receipt_claims(tmp_path) == 0
    assert path.read_text() == "original hook"
    assert not list(tmp_path.glob("*.claimed.tmp"))


@pytest.mark.unit
@pytest.mark.parametrize("owner", ["previous", "previous-drain"])
def test_orphan_claim_restores_in_one_pass(tmp_path: Path, owner: str) -> None:
    path = tmp_path / "n-1-original.json"
    claimed = path.with_name(f"{path.name}.{owner}.claimed.tmp")
    claimed.write_text("original")
    assert inbox._restore_orphaned_receipt_claims(tmp_path) == 1
    assert path.read_text() == "original"
    assert not claimed.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ghook_removal_during_decision_does_not_resurrect_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text(json.dumps({"schema_version": 1, "hook_type": "stop", "source": "codex"}))

    def settle(*args: Any, **kwargs: Any) -> ReplayDecision:
        assert path.exists()
        mark_envelope_processed("n-1-original", processed_dir=get_processed_envelope_dir(tmp_path))
        path.unlink(missing_ok=True)  # ghook's non-receipted settlement.
        return ReplayDecision("retain")

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", settle)
    app = FastAPI()
    assert await inbox.drain_hook_inbox_once(app, tmp_path, include_fresh=True) == 0
    assert not path.exists()
    assert not list(tmp_path.glob("*.claimed.tmp"))


@pytest.mark.unit
def test_invalid_claim_quarantines_under_original_name(tmp_path: Path) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text("invalid json")
    assert inbox_envelopes.load_envelope(path) is None
    quarantine = inbox_envelopes.get_hook_quarantine_dir(tmp_path)
    assert (quarantine / path.name).read_text() == "invalid json"
    assert (
        json.loads((quarantine / f"{path.name}.meta.json").read_text())["reason"] == "invalid_json"
    )
    assert not path.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_barrier_timeout_reports_active_claim_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "n-1-original.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hook_type": "stop",
                "source": "codex",
                "headers": {"X-Gobby-Agent-Run-Id": "active-run"},
            }
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def post(*args: Any, **kwargs: Any) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200)

    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "test-token")
    monkeypatch.setattr(inbox, "decide_replay", lambda *a, **kw: ReplayDecision("execute"))
    monkeypatch.setattr(inbox, "_post_envelope", post)
    # The barrier must share the app-scoped lock with the in-flight drain.
    app = FastAPI()
    holder = asyncio.create_task(inbox.drain_hook_inbox_once(app, tmp_path, include_fresh=True))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        result = await inbox.drain_hook_inbox_barrier(app, tmp_path, timeout_seconds=0)
        assert result.timed_out
        assert result.residue_hook_count == 1
        assert result.unresolved_run_ids == ("active-run",)
    finally:
        release.set()
        await holder
