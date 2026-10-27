"""The public readonly session query has an installed, audited reference."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_query_readonly_is_mapped_to_a_sessions_reference() -> None:
    audit = json.loads((ROOT / "docs/reference-audit/sessions.json").read_text())
    operations = [
        operation for operation in audit["operations"] if "query_readonly" in json.dumps(operation)
    ]
    assert len(operations) == 1, "query_readonly needs its public operation mapping"
    reference = operations[0]["reference"]
    assert reference.startswith("src/gobby/install/shared/skills/gobby/references/sessions/")
    content = (ROOT / reference).read_text()
    assert "query_readonly" in content
