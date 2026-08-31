"""Acceptance 2.5.12: daemon-held lease is the only grant point."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def test_single_grant_point_across_all_paths() -> None:
    registry = TerminalLeaseRegistry()
    first = registry.attach("term-1", frame_delivery="proxy")
    granted = registry.take_control("term-1", first.attachment_id, takeover=False)
    assert granted.granted is True
    second = registry.attach("term-1", frame_delivery="direct")
    held = registry.take_control("term-1", second.attachment_id, takeover=False)
    assert held.granted is False
    assert held.reason == "held"
    takeover = registry.take_control("term-1", second.attachment_id, takeover=True)
    assert takeover.granted is True
    assert takeover.lease_generation > granted.lease_generation
    assert registry.holder("term-1") == second.attachment_id
    assert _lease_replicas() == [], f"lease replicas exist: {_lease_replicas()}"


# Lease bookkeeping a replica of the terminal grant point would carry.
LEASE_FIELDS = ("lease_token", "lease_ttl", "lease_expiry")

# The terminal-lease grant surface. A replica has to name what it grants
# control of, so it names an attachment. Other subsystems run leases of their
# own - the hook envelope processing lease in hooks/envelope_dedupe.py is one -
# and a bare scan for LEASE_FIELDS cannot tell those from a terminal replica.
GRANT_SURFACE = ("attachment_id", "take_control", "lease_generation")


def _replica_fields(text: str) -> list[str]:
    """Lease fields in one file, or nothing when it is not a terminal grant."""
    if not any(marker in text for marker in GRANT_SURFACE):
        return []
    return [needle for needle in LEASE_FIELDS if needle in text]


def _lease_replicas() -> list[str]:
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".rs"}:
            continue
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        if path.name == "test_lease_authority.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits.extend(f"{path.relative_to(ROOT)}:{field}" for field in _replica_fields(text))
    return hits


def test_the_scoped_guard_keeps_its_teeth() -> None:
    """Scoping must not hollow out the guard, and must spare unrelated leases."""
    assert _replica_fields("attachment_id = 'a'\nlease_token = 'stolen-grant'\n") == ["lease_token"]
    assert _replica_fields("take_control()\nlease_ttl = 30\n") == ["lease_ttl"]

    # The hook envelope processing lease: its own lease, no terminal grant.
    envelope_dedupe = (ROOT / "src/gobby/hooks/envelope_dedupe.py").read_text(encoding="utf-8")
    assert "lease_expiry" in envelope_dedupe
    assert _replica_fields(envelope_dedupe) == []
