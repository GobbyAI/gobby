"""S3.2 naming switch removes Python outright: plans carry no gobby-backend model (#24954)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

PLANS = Path(__file__).resolve().parents[2] / ".gobby/plans"
VERBS_PLAN = PLANS / "client-operator-verbs.md"
MESSAGING_PLAN = PLANS / "gobby-messaging.md"


def _inventory() -> dict[str, list[dict[str, Any]]]:
    blocks = re.findall(r"^```toml\n(.*?)^```", VERBS_PLAN.read_text(), re.S | re.M)
    assert len(blocks) == 1
    return tomllib.loads(blocks[0])


def _strings(row: dict[str, Any]) -> list[str]:
    return [value for value in row.values() if isinstance(value, str)]


def test_kept_commands_name_a_rust_carrier_or_owning_ref() -> None:
    for row in _inventory()["command"]:
        if row["disposition"] == "drop":
            assert re.fullmatch(r"#\d+", row["ref"]), row
            continue
        assert row["carrier"], row
        assert not row["carrier"].startswith("gobby-backend"), row
        if row["carried"] is False:
            assert re.fullmatch(r"#\d+", row["ref"]), row


def test_no_inventory_row_mentions_gobby_backend() -> None:
    inventory = _inventory()
    offenders = [
        row
        for row in inventory["command"] + inventory["helper"]
        if any("gobby-backend" in value for value in _strings(row))
    ]
    assert offenders == []


def test_verbs_grammar_drops_gobby_backend_carrier_rule() -> None:
    assert "`gobby-backend <path>` carriers" not in VERBS_PLAN.read_text()


def test_messaging_plan_has_no_gobby_backend_or_supervised_backend() -> None:
    text = MESSAGING_PLAN.read_text()
    assert "gobby-backend" not in text
    assert re.search(r"supervised[- ]backend", text, re.I) is None
