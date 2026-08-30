"""Envelope processing markers are verifiable ownership leases."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest

from gobby.hooks import envelope_dedupe
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_REPLAY_GRACE_SECONDS,
    claim_envelope_processing,
    clear_stale_envelope_processing_marker,
    is_envelope_processing_active,
    mark_envelope_processed,
    read_envelope_marker,
    release_envelope_processing_claim,
)

pytestmark = pytest.mark.unit


def _marker_record(processed_dir: Path, envelope_id: str) -> dict[str, Any]:
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    assert record is not None
    return record


def _rewrite_marker(processed_dir: Path, envelope_id: str, **updates: object) -> dict[str, Any]:
    record = _marker_record(processed_dir, envelope_id)
    record.update(updates)
    digest = None
    for path in processed_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("envelope_id") == envelope_id:
            path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            digest = path
            break
    assert digest is not None
    return record


def test_claim_writes_owner_token_and_process_identity(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-lease"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True

    record = _marker_record(processed_dir, envelope_id)
    token = record.get("owner_token")
    assert isinstance(token, str) and token
    assert record.get("owner_pid") == os.getpid()
    create_time = record.get("owner_create_time")
    assert isinstance(create_time, float)
    assert abs(create_time - psutil.Process().create_time()) < 1.0
    assert isinstance(record.get("renewed_at"), str) and record["renewed_at"]
    assert isinstance(record.get("lease_expires_at"), str) and record["lease_expires_at"]
    expires_at = datetime.fromisoformat(str(record["lease_expires_at"]))
    assert expires_at > datetime.now(UTC)


def test_clear_stale_retains_live_owner_past_replay_grace(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-live"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    aged = (datetime.now(UTC) - timedelta(seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 30)).isoformat()
    _rewrite_marker(
        processed_dir,
        envelope_id,
        claimed_at=aged,
        renewed_at=aged,
        lease_expires_at=aged,
    )

    assert is_envelope_processing_active(envelope_id, processed_dir=processed_dir) is True
    assert clear_stale_envelope_processing_marker(envelope_id, processed_dir=processed_dir) is False
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is not None


def test_clear_stale_reclaims_expired_lease_when_owner_is_dead(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-dead"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    aged = (datetime.now(UTC) - timedelta(seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 30)).isoformat()
    _rewrite_marker(
        processed_dir,
        envelope_id,
        claimed_at=aged,
        renewed_at=aged,
        lease_expires_at=aged,
        owner_pid=2_147_483_646,
        owner_create_time=0.0,
    )

    assert is_envelope_processing_active(envelope_id, processed_dir=processed_dir) is False
    assert clear_stale_envelope_processing_marker(envelope_id, processed_dir=processed_dir) is True
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None


def test_finalize_processed_compare_and_set_rejects_losing_owner(tmp_path: Path) -> None:
    finalize = getattr(envelope_dedupe, "finalize_envelope_processed", None)
    assert finalize is not None

    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-cas"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    token = str(_marker_record(processed_dir, envelope_id)["owner_token"])

    assert (
        finalize(
            envelope_id,
            "not-the-owner",
            response={"continue": True},
            processed_dir=processed_dir,
            hook_type="PreInvocation",
        )
        is False
    )
    record = _marker_record(processed_dir, envelope_id)
    assert record.get("status") == "processing"
    assert record.get("response") is None

    assert (
        finalize(
            envelope_id,
            token,
            response={"continue": True, "decision": "allow"},
            processed_dir=processed_dir,
            hook_type="PreInvocation",
        )
        is True
    )
    record = _marker_record(processed_dir, envelope_id)
    assert record.get("status") == "processed"
    assert record.get("response") == {"continue": True, "decision": "allow"}
    assert (
        finalize(
            envelope_id,
            token,
            response={"continue": False},
            processed_dir=processed_dir,
        )
        is False
    )
    assert _marker_record(processed_dir, envelope_id).get("response") == {
        "continue": True,
        "decision": "allow",
    }


def test_release_compare_and_set_rejects_losing_owner(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-release-cas"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    token = _marker_record(processed_dir, envelope_id).get("owner_token")
    assert isinstance(token, str) and token

    assert (
        release_envelope_processing_claim(
            envelope_id,
            owner_token="not-the-owner",
            processed_dir=processed_dir,
        )
        is False
    )
    assert _marker_record(processed_dir, envelope_id).get("status") == "processing"

    assert (
        release_envelope_processing_claim(
            envelope_id,
            owner_token=token,
            processed_dir=processed_dir,
        )
        is True
    )
    assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None


def test_renew_lease_extends_expiry_for_matching_owner(tmp_path: Path) -> None:
    renew = getattr(envelope_dedupe, "renew_envelope_processing_lease", None)
    assert renew is not None

    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-renew"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    original = _marker_record(processed_dir, envelope_id)
    token = str(original["owner_token"])
    original_expiry = str(original["lease_expires_at"])

    assert renew(envelope_id, "not-the-owner", processed_dir=processed_dir) is False
    assert _marker_record(processed_dir, envelope_id)["lease_expires_at"] == original_expiry

    assert renew(envelope_id, token, processed_dir=processed_dir) is True
    renewed = _marker_record(processed_dir, envelope_id)
    assert str(renewed["lease_expires_at"]) >= original_expiry
    assert str(renewed["renewed_at"]) >= str(original["renewed_at"])


def test_mark_processed_without_token_does_not_override_foreign_lease(tmp_path: Path) -> None:
    processed_dir = tmp_path / "processed"
    envelope_id = "n-0000000000001-no-token"
    assert claim_envelope_processing(envelope_id, processed_dir=processed_dir) is True
    token = _marker_record(processed_dir, envelope_id).get("owner_token")
    assert isinstance(token, str) and token

    mark_envelope_processed(
        envelope_id,
        response={"continue": False},
        processed_dir=processed_dir,
    )
    record = _marker_record(processed_dir, envelope_id)
    assert record.get("status") == "processing"
    assert record.get("owner_token") == token
    assert record.get("response") is None
