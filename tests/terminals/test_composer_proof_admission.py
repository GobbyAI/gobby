"""Execution identity refusal checks use fake specifications, never native binaries/auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_admission import Admission

pytestmark = pytest.mark.unit


def admission_data(now: datetime) -> dict[str, object]:
    binary = {"path": "/proof/fake-binary", "sha256": "a" * 64}
    return {
        "grant": "PD_AUTHORIZED_ISOLATED_EXECUTION",
        "reviewed_commit": "a" * 40,
        "reviewed_tree": "b" * 40,
        "issued_at": now,
        "exclusion_message_id": str(uuid4()),
        "complete_exclusion_map": True,
        "excluded_identities": [str(uuid4()) for _ in range(26)],
        "native": dict.fromkeys(("gcode", "gdaemon", "ghook", "gterm", "gclient"), binary),
        "providers": {
            name: {**binary, "auth_reference": "/proof/fake-auth"} for name in ("claude", "codex")
        },
        "support_tools": [],
    }


def test_admission_binds_fresh_complete_map_to_reviewed_source() -> None:
    now = datetime.now(UTC)
    spec = Admission.model_validate(admission_data(now))
    spec.verify(now=now, commit="a" * 40, tree="b" * 40)
    cases: tuple[dict[str, object], ...] = (
        {"issued_at": now - timedelta(minutes=6)},
        {"reviewed_tree": "c" * 40},
        {"providers": {}},
        {"native": {}},
    )
    for changes in cases:
        candidate = Admission.model_validate({**admission_data(now), **changes})
        with pytest.raises(ProofRefused):
            candidate.verify(now=now, commit="a" * 40, tree="b" * 40)


@pytest.mark.parametrize(
    "change",
    [
        {"grant": "implementation-only"},
        {"complete_exclusion_map": False},
        {"excluded_identities": []},
        {"raw_secret": "fake-secret"},
    ],
)
def test_incomplete_or_secret_bearing_grants_are_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Admission.model_validate({**admission_data(datetime.now(UTC)), **change})
