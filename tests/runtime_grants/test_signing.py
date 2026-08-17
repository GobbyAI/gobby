"""Canonical grant bytes and signature comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.runtime_grants import GrantBundle, sign_grant
from gobby.runtime_grants.signing import signature_matches
from tests.runtime_grants.support import GOLDEN_SECRET

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).resolve().parent / "golden" / "direct_datastores.json"


def _unsigned_with_password(password: str) -> GrantBundle:
    grant = GrantBundle.model_validate_json(_GOLDEN.read_bytes())
    falkordb = grant.capabilities.falkordb.model_copy(update={"password": password})
    capabilities = grant.capabilities.model_copy(update={"falkordb": falkordb})
    return grant.model_copy(
        update={"capabilities": capabilities, "payload_checksum": "", "signature": ""}
    )


def test_model_dump_canonical_emits_raw_utf8() -> None:
    password = "sécret-пароль"
    signed = sign_grant(_unsigned_with_password(password), GOLDEN_SECRET)
    canonical = signed.model_dump_canonical()

    assert password.encode() in canonical
    assert r"\u00e9" not in canonical.decode()
    assert signature_matches(signed, GOLDEN_SECRET)


def test_signature_matches_returns_false_for_non_ascii_signature() -> None:
    signed = sign_grant(_unsigned_with_password("secret"), GOLDEN_SECRET)
    forged = signed.model_copy(update={"signature": "not-ascii-✓" + ("a" * 50)})

    assert signature_matches(forged, GOLDEN_SECRET) is False
