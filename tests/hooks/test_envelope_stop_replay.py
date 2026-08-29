from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.hooks.envelope_dedupe import (
    ENVELOPE_REPLAY_GRACE_SECONDS,
    bump_stop_replay_epoch,
    envelope_terminal_response,
    mark_envelope_processed,
    read_envelope_marker,
)

pytestmark = pytest.mark.unit

_STOP_BLOCK = {
    "continue": True,
    "decision": "block",
    "reason": "Rule enforced by Gobby: [block-terminal-validation-failure]\nfix it",
}
_PRETOOL_BLOCK = {"continue": False, "decision": "block", "reason": "commit required"}


def _age_marker(processed_dir: Path, envelope_id: str, *, seconds: float) -> None:
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    assert record is not None
    aged = datetime.now(UTC) - timedelta(seconds=seconds)
    record["processed_at"] = aged.isoformat()
    marker = next(path for path in processed_dir.glob("*.json"))
    marker.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")


def test_envelope_terminal_response_replays_fresh_stop_block(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    mark_envelope_processed(
        "n-0000000000001-stop",
        response=_STOP_BLOCK,
        processed_dir=processed_dir,
        hook_type="stop",
    )

    assert (
        envelope_terminal_response("n-0000000000001-stop", processed_dir=processed_dir)
        == _STOP_BLOCK
    )


def test_envelope_terminal_response_drops_aged_stop_block(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000002-stop"
    mark_envelope_processed(
        envelope_id,
        response=_STOP_BLOCK,
        processed_dir=processed_dir,
        hook_type="Stop",
    )
    _age_marker(processed_dir, envelope_id, seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 1)

    assert envelope_terminal_response(envelope_id, processed_dir=processed_dir) is None
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None


def test_envelope_terminal_response_keeps_aged_pretool_block(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000003-pretool"
    mark_envelope_processed(
        envelope_id,
        response=_PRETOOL_BLOCK,
        processed_dir=processed_dir,
        hook_type="PreToolUse",
    )
    _age_marker(processed_dir, envelope_id, seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 30)

    assert envelope_terminal_response(envelope_id, processed_dir=processed_dir) == _PRETOOL_BLOCK


def test_session_start_epoch_drops_fresh_stop_block(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000004-stop"
    mark_envelope_processed(
        envelope_id,
        response=_STOP_BLOCK,
        processed_dir=processed_dir,
        hook_type="stop",
    )
    _age_marker(processed_dir, envelope_id, seconds=15)
    bump_stop_replay_epoch(processed_dir=processed_dir, now=datetime.now(UTC))

    assert envelope_terminal_response(envelope_id, processed_dir=processed_dir) is None
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None


def test_legacy_found_work_stop_without_hook_type_expires(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000005-legacy"
    mark_envelope_processed(
        envelope_id,
        response=_STOP_BLOCK,
        processed_dir=processed_dir,
    )
    _age_marker(processed_dir, envelope_id, seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 5)

    assert envelope_terminal_response(envelope_id, processed_dir=processed_dir) is None
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None
