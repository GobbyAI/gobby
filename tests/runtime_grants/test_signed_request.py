"""Signed-request primitives: the derived key, what the MAC covers, and single-use nonces."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest

from gobby.runtime_grants.signed_request import (
    SIGNED_REQUEST_LABEL,
    NonceStore,
    derive_signed_request_key,
    signed_request_mac,
)

pytestmark = pytest.mark.unit

SIGNATURE = bytes(range(32))
PAYLOAD = "eyJzZXNzaW9uX2lkIjoic2Vzc2lvbi0xIn0"
OTHER_PAYLOAD = "eyJzZXNzaW9uX2lkIjoic2Vzc2lvbi0yIn0"
KEY = derive_signed_request_key(SIGNATURE)


def _mac(
    key: bytes = KEY,
    *,
    operation: str = "restart",
    method: str = "POST",
    path: str = "/api/admin/restart",
    query: tuple[tuple[str, str], ...] = (("force", "false"), ("terminals", "false")),
    body: bytes = b"",
    nonce: str = "nonce-1",
    encoded_payload: str = PAYLOAD,
) -> str:
    return signed_request_mac(
        key,
        operation=operation,
        method=method,
        path=path,
        query=query,
        body=body,
        nonce=nonce,
        encoded_payload=encoded_payload,
    )


def test_derived_key_is_a_sha256_over_the_label_and_the_signature() -> None:
    expected = hashlib.sha256(SIGNED_REQUEST_LABEL.encode() + SIGNATURE).digest()

    assert derive_signed_request_key(SIGNATURE) == expected


def test_derived_key_is_not_an_hmac_of_the_signature() -> None:
    # The public challenge answers HMAC(signature, nonce) for any nonce a caller picks.
    # A key of that shape would be handed to any relay that asks.
    leaked = hmac.new(SIGNATURE, SIGNED_REQUEST_LABEL.encode(), hashlib.sha256).digest()

    assert derive_signed_request_key(SIGNATURE) != leaked


def test_the_mac_is_stable_for_identical_inputs() -> None:
    assert _mac() == _mac()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("operation", "protected-runs", id="operation-swapped"),
        pytest.param("method", "GET", id="method-changed"),
        pytest.param("path", "/api/admin/shutdown", id="path-changed"),
        pytest.param("query", (("force", "true"), ("terminals", "false")), id="force-flipped"),
        pytest.param("query", (("force", "false"), ("terminals", "true")), id="terminals-flipped"),
        pytest.param("body", b'{"force": true}', id="body-added"),
        pytest.param("nonce", "nonce-2", id="nonce-changed"),
        pytest.param("encoded_payload", OTHER_PAYLOAD, id="payload-swapped"),
    ],
)
def test_the_mac_refuses_any_single_field_change(field: str, value: Any) -> None:
    changed = _mac(**{field: value})

    assert changed != _mac()


def test_the_mac_depends_on_the_derived_key() -> None:
    other_key = derive_signed_request_key(bytes(reversed(SIGNATURE)))

    assert _mac(other_key) != _mac()


def test_nonce_is_single_use() -> None:
    store = NonceStore()
    nonce = store.issue(PAYLOAD)

    assert nonce is not None
    assert store.consume(nonce, PAYLOAD) is True
    assert store.consume(nonce, PAYLOAD) is False


def test_nonce_expires_thirty_seconds_after_issue() -> None:
    now = [100.0]
    store = NonceStore(clock=lambda: now[0])
    nonce = store.issue(PAYLOAD)
    now[0] = 131.0

    assert nonce is not None
    assert store.consume(nonce, PAYLOAD) is False


def test_nonce_is_bound_to_the_payload_it_was_issued_for() -> None:
    store = NonceStore()
    nonce = store.issue(PAYLOAD)

    assert nonce is not None
    assert store.consume(nonce, OTHER_PAYLOAD) is False


def test_unknown_nonce_is_refused() -> None:
    assert NonceStore().consume("never-issued", PAYLOAD) is False


def test_nonce_store_caps_outstanding_nonces() -> None:
    store = NonceStore()
    issued = [store.issue(PAYLOAD) for _ in range(256)]

    assert all(nonce is not None for nonce in issued)
    assert store.issue(PAYLOAD) is None
