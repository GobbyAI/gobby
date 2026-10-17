"""Golden round-trip and contract-document checks for the hub-node command corpus."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.agents.spawn_models import SpawnRequest
from gobby.hub.node_commands import (
    _PARSERS,
    CONTRACT_VERSION,
    SpawnPayload,
    parse_message,
    round_trip_message,
)

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "crates" / "gdaemon" / "tests" / "fixtures" / "node_commands" / "corpus.json"
_DOC = _ROOT / "docs" / "contracts" / "node-commands.md"
_RUN = "22222222-2222-4222-8222-222222222222"
_MACHINE = "77777777-7777-4777-8777-777777777777"
# Requests and node bodies carry `v`; answers and the stale refusal do not.
_VERSIONED_KINDS = {
    "command",
    "reply",
    "checkout",
    "report",
    "prompt_delivery_failure",
    "code_index_warning",
    "terminate",
}
_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def _messages() -> list[dict[str, str]]:
    loaded = json.loads(_CORPUS.read_text(encoding="utf-8"))
    messages = loaded["messages"]
    assert isinstance(messages, list)
    assert messages
    return messages


def _message(name: str) -> dict[str, Any]:
    for message in _messages():
        if message["name"] == name:
            loaded = json.loads(message["json"])
            assert isinstance(loaded, dict)
            return loaded
    raise AssertionError(f"corpus has no message {name}")


def _encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _with_unknown_field(raw: str) -> str:
    if not raw.endswith("}"):
        raise AssertionError("corpus message is not a JSON object")
    return raw[:-1] + ',"unexpected":1}'


def _replaced(value: dict[str, Any], path: tuple[str | int, ...], new: object) -> dict[str, Any]:
    candidate = copy.deepcopy(value)
    target: Any = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = new
    return candidate


def _refuses(kind: str, value: object) -> None:
    with pytest.raises(ValidationError):
        parse_message(kind, _encode(value))


def _list_items(section: str) -> list[str]:
    items: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            items.append(line[2:])
        elif items and line.startswith("  ") and line.strip():
            items[-1] += " " + line.strip()
    return items


def _schema_field_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            names |= _schema_field_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _schema_field_names(value)
    return names


def test_corpus_round_trips() -> None:
    kinds = set()
    for message in _messages():
        kinds.add(message["kind"])
        value = json.loads(message["json"])
        if message["kind"] in _VERSIONED_KINDS:
            assert value["v"] == CONTRACT_VERSION, message["name"]
        else:
            assert "v" not in value, message["name"]
        encoded = round_trip_message(message["kind"], message["json"])
        assert encoded == message["json"], message["name"]
        with pytest.raises(ValidationError):
            parse_message(message["kind"], _with_unknown_field(message["json"]))
    assert kinds == set(_PARSERS)


def test_node_bodies_name_no_machine() -> None:
    checkout: dict[str, object] = {
        "v": CONTRACT_VERSION,
        "project_id": "55555555-5555-4555-8555-555555555555",
        "root_path": "/Users/josh/Projects/gobby",
        "machine_id": _MACHINE,
    }
    _refuses("checkout", checkout)

    report: dict[str, object] = {
        "v": CONTRACT_VERSION,
        "hub_epoch": None,
        "revision": 1,
        "host_epoch": 4,
        "accepted": [],
        "agents": [],
        "exits": [],
        "machine_id": _MACHINE,
    }
    _refuses("report", report)

    for name, kind in (
        ("runs.prompt_delivery_failure", "prompt_delivery_failure"),
        ("runs.code_index_warning", "code_index_warning"),
        ("runs.terminate", "terminate"),
    ):
        body = _message(name)
        parse_message(kind, _encode(body))
        _refuses(kind, _replaced(body, ("machine_id",), _MACHINE))

    spawn = _message("command.agent.spawn")
    parse_message("command", _encode(spawn))
    _refuses("command", _replaced(spawn, ("payload", "sandbox", "network"), "trusted"))
    _refuses("command", _replaced(spawn, ("payload", "env"), {"PATH": "/usr/bin"}))
    _refuses("command", _replaced(spawn, ("payload", "cwd"), "/tmp/work"))

    existing = _message("command.agent.spawn.existing")
    assert existing["payload"]["workspace"]["mode"] == "existing"
    parse_message("command", _encode(existing))
    _refuses("command", _replaced(existing, ("payload", "workspace", "path"), None))
    assert spawn["payload"]["workspace"]["mode"] == "worktree"
    _refuses("command", _replaced(spawn, ("payload", "workspace", "path"), "/tmp/work"))

    too_many: dict[str, object] = {
        "v": CONTRACT_VERSION,
        "hub_epoch": None,
        "revision": 1,
        "host_epoch": 4,
        "accepted": [_RUN] * 1001,
        "agents": [],
        "exits": [],
    }
    _refuses("report", too_many)


def test_contract_doc_covers_version_2() -> None:
    text = _DOC.read_text(encoding="utf-8")
    fields: set[str] = set()
    for adapter in _PARSERS.values():
        fields |= _schema_field_names(adapter.json_schema())
    assert {"pane", "attention", "denial_operations", "recent_violations", "monitor"} <= fields
    missing = sorted(name for name in fields if f"`{name}`" not in text)
    assert missing == []
    for phrase in (
        "CONTRACT_VERSION",
        "POST /api/nodes/runs/prompt-delivery-failure",
        "POST /api/nodes/runs/code-index-warning",
        "POST /api/nodes/runs/terminate",
        "GET /api/nodes/detection-manifests",
        "`existing`",
        "run_not_found",
        "remote_loopback_endpoint",
        "spawn_agent requires managed SRT (enabled=<enabled>, backend=<backend>)",
        "300 seconds",
    ):
        assert phrase in text, phrase
    assert "remote_resume_unsupported" not in text


def test_fields_not_carried_matches_spawn_request() -> None:
    text = _DOC.read_text(encoding="utf-8")
    start = text.index("\n## Fields not carried\n")
    end = text.find("\n## ", start + 1)
    items = _list_items(text[start : end if end != -1 else len(text)])
    assert items
    documented: set[str] = set()
    for item in items:
        head, colon, reason = item.partition(":")
        names = re.findall(r"`([^`]+)`", head)
        assert colon, item
        assert names, item
        assert reason.strip(), item
        documented.update(names)
    expected = {field.name for field in dataclasses.fields(SpawnRequest)} - set(
        SpawnPayload.model_fields
    )
    assert documented == expected


def test_report_digests_and_sandbox_records_validate() -> None:
    report = _message("report.full")
    attention = report["agents"][0]["pane"]["attention"]
    sandbox = report["agents"][0]["sandbox"]
    assert sandbox["runtime_version"] == "0.0.79"
    assert set(sandbox["denial_operations"]) == {"file-write-create", "file-write*", "unclassified"}
    parsed = json.loads(round_trip_message("report", _encode(report)))
    parsed_attention = parsed["agents"][0]["pane"]["attention"]
    assert parsed_attention["fingerprint"] == attention["fingerprint"]
    assert parsed_attention["payload"]["fingerprint"] == attention["payload"]["fingerprint"]
    assert parsed["agents"][0]["sandbox"] == sandbox
    assert parsed["exits"][0]["sandbox"] == report["exits"][0]["sandbox"]

    attention_path: tuple[str | int, ...] = ("agents", 0, "pane", "attention")
    agent_sandbox: tuple[str | int, ...] = ("agents", 0, "sandbox")
    exit_sandbox: tuple[str | int, ...] = ("exits", 0, "sandbox")
    cases: list[tuple[tuple[str | int, ...], object]] = []
    for digest in (_SECRET, "a" * 63, "A" * 64):
        cases.append(((*attention_path, "fingerprint"), digest))
        cases.append(((*attention_path, "payload", "fingerprint"), digest))
        cases.append(((*agent_sandbox, "policy_hash"), digest))
        cases.append(((*exit_sandbox, "policy_hash"), digest))
    cases.extend(
        [
            ((*agent_sandbox, "runtime_version"), _SECRET),
            ((*agent_sandbox, "runtime_version"), "0.0.79-x"),
            ((*agent_sandbox, "denial_operations"), {"File-Write": 1}),
            ((*agent_sandbox, "denial_operations"), {"sk-abcdefghijklmnopqrstuv": 1}),
            ((*agent_sandbox, "denial_operations"), {"unclassified": -1}),
            ((*exit_sandbox, "recent_violations"), [{"sequence": n} for n in range(101)]),
        ]
    )
    for path, value in cases:
        _refuses("report", _replaced(report, path, value))
