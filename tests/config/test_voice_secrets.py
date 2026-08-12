from __future__ import annotations

from collections.abc import Callable

import pytest

from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.voice_secrets import (
    restore_masked_structured_references,
    validate_structured_references,
)


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
