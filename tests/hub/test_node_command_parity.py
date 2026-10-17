"""Version 3 command parity and shared refusals for the hub-node corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.hub.node_commands import (
    CONTRACT_VERSION,
    MergeCommand,
    MergeResult,
    NodeModel,
    OkReply,
    RemoveCommand,
    RemoveResult,
    StatusCommand,
    StatusResult,
    parse_message,
    round_trip_message,
)

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "crates" / "gdaemon" / "tests" / "fixtures" / "node_commands" / "corpus.json"
# The version 3 commands and one reply per outcome, with each one's v3 model.
_V3_MESSAGES: tuple[tuple[str, type[NodeModel]], ...] = (
    ("command.workspace.merge", MergeCommand),
    ("command.workspace.remove", RemoveCommand),
    ("command.checkout.status", StatusCommand),
    ("reply.workspace.merge.merged", MergeResult),
    ("reply.workspace.merge.already_merged", MergeResult),
    ("reply.workspace.merge.conflict", MergeResult),
    ("reply.workspace.merge.target_dirty", MergeResult),
    ("reply.workspace.merge.busy", MergeResult),
    ("reply.workspace.remove.removed", RemoveResult),
    ("reply.workspace.remove.dirty", RemoveResult),
    ("reply.workspace.remove.unmerged", RemoveResult),
    ("reply.workspace.remove.busy", RemoveResult),
    ("reply.checkout.status", StatusResult),
    ("reply.checkout.status.nulls", StatusResult),
)


def _corpus_list(key: str) -> list[dict[str, str]]:
    loaded = json.loads(_CORPUS.read_text(encoding="utf-8"))
    entries = loaded[key]
    assert isinstance(entries, list)
    return entries


def test_v3_commands_round_trip() -> None:
    messages = {message["name"]: message for message in _corpus_list("messages")}
    for name, model in _V3_MESSAGES:
        message = messages[name]
        parsed = parse_message(message["kind"], message["json"])
        if isinstance(parsed, OkReply):
            assert parsed.v == CONTRACT_VERSION, name
            assert isinstance(parsed.result, model), name
        else:
            assert isinstance(parsed, model), name
            assert isinstance(parsed, MergeCommand | RemoveCommand | StatusCommand), name
            assert parsed.v == CONTRACT_VERSION, name
        encoded = round_trip_message(message["kind"], message["json"])
        assert encoded == message["json"], name


def test_invalid_messages_fail() -> None:
    invalid = _corpus_list("invalid")
    assert len(invalid) == 5
    for message in invalid:
        with pytest.raises(ValidationError):
            parse_message(message["kind"], message["json"])
