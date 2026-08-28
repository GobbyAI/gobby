"""Acceptance 2.5.17 / 2.5.24–27 / 2.5.36: terminal WS golden corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.terminals.ws_protocol import (
    GOLDEN_NAMES,
    TERMINAL_WS_SAFE_INTEGER_MAX,
    decode_message,
    encode_message,
    fragment_event,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "terminal_ws_golden"

pytestmark = pytest.mark.unit


def _load(name: str) -> bytes:
    return (GOLDEN_DIR / name).read_bytes()


def test_python_matches_terminal_ws_golden_corpus() -> None:
    missing = [name for name in GOLDEN_NAMES if not (GOLDEN_DIR / name).is_file()]
    assert missing == [], f"golden corpus missing {missing}"
    for name in GOLDEN_NAMES:
        raw = _load(name)
        message = decode_message(raw)
        assert encode_message(message) == raw, name
        parsed = json.loads(raw)
        assert "mode" not in parsed
        if parsed.get("type") in {"terminal_attach", "terminal_attach_result"}:
            assert "mode" not in parsed


def test_write_outcome_is_correlated() -> None:
    delivered = json.loads(_load("write_outcome.json"))
    indeterminate = json.loads(_load("write_outcome_indeterminate.json"))
    refused = json.loads(_load("write_outcome_refused.json"))
    conflict = json.loads(_load("write_outcome_conflict.json"))
    expired = json.loads(_load("write_outcome_expired.json"))
    capacity = json.loads(_load("write_outcome_capacity.json"))
    inbound = json.loads(_load("input.json"))
    paste = json.loads(_load("paste.json"))
    assert inbound["client_write_seq"] == delivered["client_write_seq"]
    assert inbound["attachment_id"] == delivered["attachment_id"]
    assert delivered["outcome"] == "delivered"
    assert delivered["reason"] is None
    assert indeterminate["outcome"] == "indeterminate"
    assert refused["outcome"] == "refused"
    assert conflict["reason"] == "write_seq_conflict"
    assert expired["reason"] == "write_seq_expired"
    assert capacity["reason"] == "write_seq_capacity"
    assert isinstance(paste["client_write_seq"], int)


def test_attach_history_precedes_first_output() -> None:
    history = json.loads(_load("attach_history.json"))
    output = json.loads(_load("output.json"))
    assert history["type"] == "terminal_attach_history"
    assert output["type"] == "terminal_output"
    assert history["attachment_id"] == output["attachment_id"]
    assert set(history) >= {
        "terminal_id",
        "attachment_id",
        "text",
        "truncated",
        "dropped_bytes",
        "total_bytes",
    }


def test_fragment_envelope_is_pinned() -> None:
    first = json.loads(_load("fragment.json"))
    last = json.loads(_load("fragment_last.json"))
    assert first["type"] == last["type"] == "terminal_ws_fragment"
    assert first["event"] == last["event"]
    assert first["message_seq"] == last["message_seq"]
    assert first["attachment_id"] == last["attachment_id"]
    assert first["fragment_index"] == 0
    assert last["fragment_index"] == 1
    assert first["more"] is True
    assert last["more"] is False
    assert first["encoding"] == last["encoding"] == "utf8-b64"
    assert len(_load("fragment.json")) < 2 * 1024 * 1024
    assert len(_load("fragment_last.json")) < 2 * 1024 * 1024
    reconstructed = fragment_event(
        event=first["event"],
        terminal_id=first["terminal_id"],
        attachment_id=first["attachment_id"],
        message_seq=first["message_seq"],
        complete_json=_reassemble(first, last),
    )
    assert reconstructed[0]["fragment_index"] == 0


def _reassemble(first: dict[str, object], last: dict[str, object]) -> bytes:
    import base64

    return base64.b64decode(str(first["payload"])) + base64.b64decode(str(last["payload"]))


def test_seq_and_lease_generation_are_safe_integers() -> None:
    overflow = TERMINAL_WS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ValueError, match="safe_integer_overflow"):
        encode_message(
            {
                "type": "terminal_control_result",
                "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "granted": False,
                "reason": "held",
                "lease_generation": overflow,
            }
        )
    high = json.loads(_load("control_result.json"))
    assert isinstance(high["lease_generation"], int)
    assert high["lease_generation"] <= TERMINAL_WS_SAFE_INTEGER_MAX


def test_attachment_finalized_is_pinned() -> None:
    payload = json.loads(_load("attachment_finalized.json"))
    assert payload["type"] == "terminal_attachment_finalized"
    assert payload["reason"] in {
        "detach",
        "ws_close",
        "ws_loss",
        "proxy_frame_eof",
        "proxy_lag",
        "relay_overflow",
        "host_loss",
        "message_seq_overflow",
    }
    assert isinstance(payload["lease_generation"], int)
