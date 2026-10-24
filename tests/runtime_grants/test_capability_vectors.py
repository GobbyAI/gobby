"""The committed capability vectors still match Python's capabilities_from_snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gobby.runtime_grants.schema import PostgresDirect
from gobby.runtime_grants.service import capabilities_from_snapshot
from tests.runtime_grants.capability_vectors import snapshot_for

VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gruntime"
    / "tests"
    / "fixtures"
    / "capability_vectors.json"
)


def test_vectors_match_python_capabilities() -> None:
    document: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert document["cases"], "capability vectors must be generated from Python"
    postgres = PostgresDirect.model_validate(document["postgres"])
    for case in document["cases"]:
        capabilities = capabilities_from_snapshot(
            snapshot_for(case["values"], case["secrets"]),
            postgres,
        )
        assert capabilities.model_dump(mode="json") == case["expected"], case["name"]
