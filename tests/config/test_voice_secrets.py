from __future__ import annotations

from collections.abc import Callable

import pytest

from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.voice_secrets import (
    restore_masked_structured_references,
    validate_structured_references,
)

pytestmark = pytest.mark.unit


def test_masked_structured_references_follow_registry_identity_when_reordered() -> None:
    persisted = [
        {"binding_id": "first", "token": "$secret:FIRST"},
        {"binding_id": "second", "token": "$secret:SECOND"},
    ]
    submitted = [
        {"binding_id": "second", "token": MASKED_SECRET},
        {"binding_id": "first", "token": MASKED_SECRET},
    ]

    restored = restore_masked_structured_references(
        "example.bindings",
        submitted,
        persisted,
        ("token",),
        "binding_id",
    )

    assert restored == [
        {"binding_id": "second", "token": "$secret:SECOND"},
        {"binding_id": "first", "token": "$secret:FIRST"},
    ]


def test_masked_duplicate_persisted_items_fill_missing_fields() -> None:
    persisted = [
        {"provider": "openai", "api_key": "$secret:FIRST"},
        {"provider": "openai", "token": "$secret:TOKEN"},
    ]
    submitted = [
        {"provider": "openai", "api_key": MASKED_SECRET, "token": MASKED_SECRET},
    ]

    restored = restore_masked_structured_references(
        "voice.providers",
        submitted,
        persisted,
        ("api_key", "token"),
        "provider",
    )

    assert restored == [
        {
            "provider": "openai",
            "api_key": "$secret:FIRST",
            "token": "$secret:TOKEN",
        },
    ]


def test_masked_duplicate_persisted_provider_references_keep_first() -> None:
    persisted = [
        {"provider": "openai", "label": "first", "api_key": "$secret:FIRST"},
        {"provider": "openai", "label": "second", "api_key": "$secret:SECOND"},
    ]
    submitted = [
        {"provider": "openai", "label": "second", "api_key": MASKED_SECRET},
    ]

    restored = restore_masked_structured_references(
        "voice.providers",
        submitted,
        persisted,
        ("api_key",),
        "provider",
    )

    assert restored == [
        {"provider": "openai", "label": "second", "api_key": "$secret:FIRST"},
    ]


def test_masked_duplicate_incoming_provider_references_are_rejected() -> None:
    persisted = [{"provider": "openai", "api_key": "$secret:KEY_0"}]
    submitted = [
        {"provider": "openai", "api_key": MASKED_SECRET},
        {"provider": "openai", "api_key": MASKED_SECRET},
    ]

    with pytest.raises(
        ValueError,
        match="voice.providers incoming items have duplicate provider 'openai'",
    ):
        restore_masked_structured_references(
            "voice.providers",
            submitted,
            persisted,
            ("api_key",),
            "provider",
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda: validate_structured_references("example.bindings", {}, ("token",)),
        lambda: restore_masked_structured_references(
            "example.bindings", {}, [], ("token",), "binding_id"
        ),
    ],
)
def test_structured_reference_contract_requires_a_list(operation: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="example.bindings must be a list"):
        operation()


def test_masked_structured_reference_requires_declared_identity() -> None:
    with pytest.raises(ValueError, match="binding_id must be a non-empty string"):
        restore_masked_structured_references(
            "example.bindings",
            [{"token": MASKED_SECRET}],
            [{"token": "$secret:TOKEN"}],
            ("token",),
            "binding_id",
        )
