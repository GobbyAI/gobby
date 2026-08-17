"""Daemon-side HMAC signing for v2 grants."""

from __future__ import annotations

import hashlib
import hmac
import json

from gobby.runtime_grants.schema import GrantBundle


def canonical_payload_bytes(grant: GrantBundle) -> bytes:
    """Return the canonical JSON bytes covered by checksum and signature."""
    payload = grant.model_dump(mode="json", exclude={"payload_checksum", "signature"})
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def payload_checksum(grant: GrantBundle) -> str:
    """Return the sha256 hex digest of the canonical signed payload."""
    return hashlib.sha256(canonical_payload_bytes(grant)).hexdigest()


def sign_grant(grant: GrantBundle, secret: str) -> GrantBundle:
    """Sign `grant` with the deployment's stored HMAC secret."""
    payload = canonical_payload_bytes(grant)
    checksum = hashlib.sha256(payload).hexdigest()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return grant.model_copy(update={"payload_checksum": checksum, "signature": signature})


def signature_matches(grant: GrantBundle, secret: str) -> bool:
    """Return whether `grant.signature` matches the deployment secret."""
    expected = hmac.new(secret.encode(), canonical_payload_bytes(grant), hashlib.sha256).hexdigest()
    try:
        actual = grant.signature.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(actual, expected.encode("ascii"))
