"""Canonical digest helpers for plan review payloads."""

from __future__ import annotations

import hashlib
import json


def canonical_json_sha256(payload: object) -> str:
    """Return the SHA-256 digest of compact, key-sorted JSON."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
