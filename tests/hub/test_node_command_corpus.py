"""Golden round-trip for the hub-node command corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.hub.node_commands import parse_message, round_trip_message

_CORPUS = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gdaemon"
    / "tests"
    / "fixtures"
    / "node_commands"
    / "corpus.json"
)
_RUN = "22222222-2222-4222-8222-222222222222"


def _messages() -> list[dict[str, str]]:
    loaded = json.loads(_CORPUS.read_text(encoding="utf-8"))
    messages = loaded["messages"]
    assert isinstance(messages, list)
    assert messages
    return messages


def _with_unknown_field(raw: str) -> str:
    if not raw.endswith("}"):
        raise AssertionError("corpus message is not a JSON object")
    return raw[:-1] + ',"unexpected":1}'


def test_corpus_round_trips() -> None:
    for message in _messages():
        encoded = round_trip_message(message["kind"], message["json"])
        assert encoded == message["json"], message["name"]
        with pytest.raises(ValidationError):
            parse_message(message["kind"], _with_unknown_field(message["json"]))


def test_node_bodies_name_no_machine() -> None:
    checkout: dict[str, object] = {
        "v": 1,
        "project_id": "55555555-5555-4555-8555-555555555555",
        "root_path": "/Users/josh/Projects/gobby",
        "machine_id": "77777777-7777-4777-8777-777777777777",
    }
    with pytest.raises(ValidationError):
        parse_message("checkout", json.dumps(checkout, separators=(",", ":")))

    report: dict[str, object] = {
        "v": 1,
        "hub_epoch": None,
        "revision": 1,
        "host_epoch": 4,
        "accepted": [],
        "agents": [],
        "exits": [],
        "machine_id": "77777777-7777-4777-8777-777777777777",
    }
    with pytest.raises(ValidationError):
        parse_message("report", json.dumps(report, separators=(",", ":")))

    spawn = json.loads(_messages()[0]["json"])
    spawn["payload"]["env"] = {"PATH": "/usr/bin"}
    with pytest.raises(ValidationError):
        parse_message("command", json.dumps(spawn, separators=(",", ":")))
    del spawn["payload"]["env"]
    spawn["payload"]["cwd"] = "/tmp/work"
    with pytest.raises(ValidationError):
        parse_message("command", json.dumps(spawn, separators=(",", ":")))

    too_many: dict[str, object] = {
        "v": 1,
        "hub_epoch": None,
        "revision": 1,
        "host_epoch": 4,
        "accepted": [_RUN] * 1001,
        "agents": [],
        "exits": [],
    }
    with pytest.raises(ValidationError):
        parse_message("report", json.dumps(too_many, separators=(",", ":")))
