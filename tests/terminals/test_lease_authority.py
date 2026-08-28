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
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".rs"}:
            continue
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        if path.name == "test_lease_authority.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in ("lease_token", "lease_ttl", "lease_expiry"):
            if needle in text:
                hits.append(f"{path.relative_to(ROOT)}:{needle}")
    assert hits == [], f"lease replicas exist: {hits}"
