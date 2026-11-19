"""Contracts for the adapter wire recordings of the five provider-API-contract adapters.

Each fixture in ``adapter_wire/`` is named ``<provider>_<operation>.json`` and records one
operation of one Python adapter, driven in-process through the loopback provider edge with
responses scripted from the provider's published API contract. No fixture was recorded against
a real account, so none is an oracle. A fixture holds:

- ``provider`` and ``operation``: the adapter and the operation, matching the file name.
- ``capture``: ``exchange`` when the adapter reached the provider, ``refusal`` when the adapter
  refused before any provider request.
- ``verified``: always ``false``; ``oracle``: always ``false``.
- ``source_file``, ``source_test``: the adapter module and the existing adapter test whose shapes
  the recording follows (``source_test`` is a pytest node id, ``file::test`` or
  ``file::Class::test``); ``contract_ref``: the published provider contract the scripted
  responses are taken from.
- ``drive``: a non-empty object, the adapter call (channel config and the message, attachment or
  webhook payload).
- ``provider_contract``: an object whose ``responses`` list holds the scripted provider
  responses in the order the edge serves them, each with the ``host``, ``method`` and ``path``
  of the request it answers and its ``body``. A refusal scripts no response.
- ``exchanges``: the ordered provider requests, each with ``host``, ``method``, ``path`` and the
  sanitized ``semantic`` fields ``destination``, ``text``, ``filename`` and ``payload_sha256``.
- ``outcome``: the semantic result the adapter returned.
"""

from __future__ import annotations

import ast
import copy
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WIRE_DIR = Path(__file__).resolve().parent / "adapter_wire"
ADAPTER_DIR = REPO_ROOT / "src" / "gobby" / "communications" / "adapters"
ADAPTER_TEST_DIR = REPO_ROOT / "tests" / "communications"
RECORD_ENV = "GOBBY_RECORD_ADAPTER_WIRE"
PROVIDERS = ("slack", "telegram", "teams", "email", "sms")
OPERATIONS = ("send", "inbound", "attachment")
EXPECTED_FIXTURES = frozenset(f"{p}_{o}" for p in PROVIDERS for o in OPERATIONS)
PROVENANCE_KEYS = ("source_file", "source_test", "contract_ref", "oracle")
CAPTURE_KINDS = ("exchange", "refusal")
REFUSAL_FIXTURES = frozenset({"sms_attachment"})
SEMANTIC_FIELDS = ("destination", "text", "filename", "payload_sha256")


@dataclass(frozen=True)
class WireReplay:
    """What one replay of a fixture observed.

    ``exchanges`` are the provider requests the edge served, in arrival order, in the fixture's
    exchange shape. ``upstream`` lists every connect the adapter attempted to a non-loopback
    address. ``outcome`` is the semantic result the adapter returned.
    """

    exchanges: list[dict[str, Any]] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)
    outcome: Any = None


def replay_wire_fixture(fixture: dict[str, Any], edge_root: Path) -> WireReplay:
    """Drive the fixture's adapter operation through a fresh edge under ``edge_root``."""
    return WireReplay()


def wire_differences(fixture: dict[str, Any], replay: WireReplay) -> list[str]:
    """Every difference between a fixture and a replay, one line per differing leaf.

    Each line starts with the JSON pointer of the differing leaf followed by ``": "``, for
    example ``/exchanges/2/semantic/text: ...``, ``/outcome/message_id: ...`` or
    ``/upstream: ...``. A leaf that does not differ gets no line.
    """
    return []


def record_wire_fixtures(out_dir: Path, edge_root: Path) -> list[Path]:
    """Record every expected fixture into ``out_dir`` and return the written paths."""
    return []


def _fixture_paths() -> dict[str, Path]:
    return {path.stem: path for path in sorted(WIRE_DIR.glob("*.json"))}


def _load_fixtures() -> dict[str, dict[str, Any]]:
    """Every fixture, keyed by file stem, after asserting the exact provider x operation set."""
    paths = _fixture_paths()
    missing = sorted(EXPECTED_FIXTURES - paths.keys())
    extra = sorted(paths.keys() - EXPECTED_FIXTURES)
    assert len(paths) == len(EXPECTED_FIXTURES), (
        f"{WIRE_DIR} holds {len(paths)} fixtures, not {len(EXPECTED_FIXTURES)}; "
        f"missing {missing}, unexpected {extra}"
    )
    assert not missing and not extra, f"missing fixtures {missing}, unexpected fixtures {extra}"
    fixtures: dict[str, dict[str, Any]] = {}
    for stem, path in paths.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path} is not a JSON object"
        fixtures[stem] = document
    return fixtures


def _defines_test(source: str, node_path: list[str]) -> bool:
    """Whether ``source`` defines the test at ``node_path`` (classes, then the function)."""
    *classes, function = node_path
    body: list[ast.stmt] = ast.parse(source).body
    for class_name in classes:
        found = [n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if not found:
            return False
        body = found[0].body
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        for node in body
    )


def _pointers(differences: list[str]) -> set[str]:
    return {line.partition(": ")[0] for line in differences}


def _signatures(exchanges: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(str(e.get("host")), str(e.get("method")), str(e.get("path"))) for e in exchanges]


def _synthetic_fixture() -> dict[str, Any]:
    """A fixture shape built in the test, independent of any recorded file."""
    return {
        "provider": "slack",
        "operation": "attachment",
        "capture": "exchange",
        "exchanges": [
            {
                "host": "slack.com",
                "method": "POST",
                "path": "/api/files.getUploadURLExternal",
                "semantic": {
                    "destination": "C0CORPUS0001",
                    "text": "",
                    "filename": "corpus.txt",
                    "payload_sha256": "",
                },
            },
            {
                "host": "files.slack.com",
                "method": "PUT",
                "path": "/upload/corpus",
                "semantic": {
                    "destination": "",
                    "text": "",
                    "filename": "corpus.txt",
                    "payload_sha256": "0" * 64,
                },
            },
            {
                "host": "slack.com",
                "method": "POST",
                "path": "/api/files.completeUploadExternal",
                "semantic": {
                    "destination": "C0CORPUS0001",
                    "text": "corpus attachment",
                    "filename": "corpus.txt",
                    "payload_sha256": "",
                },
            },
        ],
        "outcome": {"message_id": "F0CORPUS0001"},
    }


def _faithful_replay(fixture: dict[str, Any]) -> WireReplay:
    return WireReplay(
        exchanges=copy.deepcopy(fixture["exchanges"]),
        upstream=[],
        outcome=copy.deepcopy(fixture["outcome"]),
    )


def test_adapter_wire_covers_every_provider_and_operation() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        named = f"{fixture.get('provider')}_{fixture.get('operation')}"
        assert named == stem, f"{stem}.json declares provider and operation {named!r}"


def test_adapter_wire_fixtures_carry_provenance() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        absent = [key for key in PROVENANCE_KEYS if key not in fixture]
        assert not absent, f"{stem}.json lacks the provenance keys {absent}"
        assert fixture["oracle"] is False, f"{stem}.json does not declare oracle false"
        source_file = REPO_ROOT / str(fixture["source_file"])
        assert source_file.is_file(), f"{stem}.json names a missing source file {source_file}"
        assert source_file.parent == ADAPTER_DIR, (
            f"{stem}.json names {source_file}, which is not an adapter module"
        )
        test_path, *node_path = str(fixture["source_test"]).split("::")
        source_test = REPO_ROOT / test_path
        assert source_test.is_file() and source_test.is_relative_to(ADAPTER_TEST_DIR), (
            f"{stem}.json names {source_test}, which is not a communications test file"
        )
        assert node_path and all(node_path), (
            f"{stem}.json names the node id {fixture['source_test']!r}, which has no test name"
        )
        assert _defines_test(source_test.read_text(encoding="utf-8"), node_path), (
            f"{stem}.json names the test {'::'.join(node_path)!r}, "
            f"which {source_test} does not define"
        )
        contract_ref = str(fixture["contract_ref"])
        assert contract_ref.startswith("https://"), (
            f"{stem}.json cites {contract_ref!r}, not a published provider contract"
        )


def test_adapter_wire_labels_refusals_and_unverified_captures() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        assert fixture.get("verified") is False, f"{stem}.json does not declare verified false"
        capture = fixture.get("capture")
        assert capture in CAPTURE_KINDS, f"{stem}.json declares the capture kind {capture!r}"
        exchanges = fixture.get("exchanges")
        assert isinstance(exchanges, list), f"{stem}.json has no exchanges list"
        if stem in REFUSAL_FIXTURES:
            assert capture == "refusal", f"{stem}.json is not labelled a refusal"
        if capture == "refusal":
            assert exchanges == [], f"the refusal {stem}.json records provider requests"
            outcome = fixture.get("outcome")
            assert isinstance(outcome, dict) and outcome.get("error"), (
                f"the refusal {stem}.json records no error outcome"
            )
        elif fixture.get("operation") != "inbound":
            assert exchanges, f"{stem}.json records a {fixture.get('operation')} with no request"
        for exchange in exchanges:
            semantic = exchange.get("semantic")
            assert isinstance(semantic, dict), f"{stem}.json has an exchange with no semantics"
            assert set(semantic) == set(SEMANTIC_FIELDS), (
                f"{stem}.json carries the semantic fields {sorted(semantic)}"
            )


def test_adapter_wire_fixtures_carry_drive_and_provider_contract() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        drive = fixture.get("drive")
        assert isinstance(drive, dict) and drive, f"{stem}.json has no drive object"
        contract = fixture.get("provider_contract")
        assert isinstance(contract, dict), f"{stem}.json has no provider_contract object"
        responses = contract.get("responses")
        assert isinstance(responses, list), f"{stem}.json has no provider_contract.responses list"
        if fixture.get("capture") == "refusal":
            assert responses == [], f"the refusal {stem}.json scripts provider responses"
        for index, response in enumerate(responses):
            assert isinstance(response, dict), f"{stem}.json response {index} is not an object"
            absent = [key for key in ("host", "method", "path", "body") if key not in response]
            assert not absent, f"{stem}.json response {index} lacks {absent}"


@pytest.mark.integration
def test_adapter_wire_recordings_are_offline(tmp_path: Path) -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        replay = replay_wire_fixture(fixture, tmp_path / stem)
        assert replay.upstream == [], f"{stem} reached past the edge: {replay.upstream}"
        assert _signatures(replay.exchanges) == _signatures(fixture["exchanges"]), (
            f"{stem} sent {_signatures(replay.exchanges)}, "
            f"not the recorded {_signatures(fixture['exchanges'])}"
        )


@pytest.mark.integration
def test_adapter_wire_replay_matches_the_provider_contract(tmp_path: Path) -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        replay = replay_wire_fixture(fixture, tmp_path / stem)
        assert replay.exchanges == fixture["exchanges"], (
            f"{stem} sent {replay.exchanges}, not the recorded {fixture['exchanges']}"
        )
        assert replay.outcome == fixture["outcome"], (
            f"{stem} returned {replay.outcome!r}, not the recorded {fixture['outcome']!r}"
        )
        assert wire_differences(fixture, replay) == [], f"{stem} differs from its fixture"


@pytest.mark.integration
def test_adapter_wire_replay_detects_a_changed_provider_response(tmp_path: Path) -> None:
    fixture = copy.deepcopy(_load_fixtures()["slack_send"])
    responses = fixture.get("provider_contract", {}).get("responses")
    assert isinstance(responses, list) and responses, "slack_send.json scripts no responses"
    posts = [r for r in responses if r.get("path") == "/api/chat.postMessage"]
    assert len(posts) == 1, f"slack_send.json scripts {len(posts)} chat.postMessage responses"
    body = posts[0].get("body")
    assert isinstance(body, dict) and "ts" in body, "slack_send.json scripts no message ts"
    body["ts"] = "9999999999.999999"
    replay = replay_wire_fixture(fixture, tmp_path / "slack_send")
    differences = wire_differences(fixture, replay)
    assert "/outcome/message_id" in _pointers(differences), (
        f"a changed provider message ts gave the differences {differences}"
    )


def _swap_first_two(replay: WireReplay) -> None:
    replay.exchanges[0], replay.exchanges[1] = replay.exchanges[1], replay.exchanges[0]


def _set_semantic(name: str, value: str) -> Callable[[WireReplay], None]:
    def mutate(replay: WireReplay) -> None:
        replay.exchanges[2]["semantic"][name] = value

    return mutate


def _change_outcome(replay: WireReplay) -> None:
    replay.outcome["message_id"] = "F0CORPUS0002"


def _reach_upstream(replay: WireReplay) -> None:
    replay.upstream.append("203.0.113.10:443")


_SWAPPED_LEAVES = frozenset(
    f"/exchanges/{index}/{leaf}"
    for index in (0, 1)
    for leaf in ("host", "method", "path", "semantic/destination", "semantic/payload_sha256")
)


@pytest.mark.parametrize(
    ("mutation", "pointers"),
    [
        pytest.param(_swap_first_two, _SWAPPED_LEAVES, id="request-order"),
        pytest.param(
            _set_semantic("destination", "C0OTHER0001"),
            {"/exchanges/2/semantic/destination"},
            id="destination",
        ),
        pytest.param(
            _set_semantic("text", "another text"), {"/exchanges/2/semantic/text"}, id="text"
        ),
        pytest.param(
            _set_semantic("filename", "other.txt"),
            {"/exchanges/2/semantic/filename"},
            id="filename",
        ),
        pytest.param(
            _set_semantic("payload_sha256", "f" * 64),
            {"/exchanges/2/semantic/payload_sha256"},
            id="digest",
        ),
        pytest.param(_change_outcome, {"/outcome/message_id"}, id="outcome"),
        pytest.param(_reach_upstream, {"/upstream"}, id="upstream"),
    ],
)
def test_wire_differences_names_a_mutated_field(
    mutation: Callable[[WireReplay], None], pointers: frozenset[str] | set[str]
) -> None:
    fixture = _synthetic_fixture()
    replay = _faithful_replay(fixture)
    assert wire_differences(fixture, replay) == [], "a faithful replay gave differences"
    mutation(replay)
    differences = wire_differences(fixture, replay)
    assert differences, f"a mutation of {sorted(pointers)} gave no difference"
    assert _pointers(differences) == set(pointers), (
        f"a mutation of {sorted(pointers)} gave the differences {differences}, "
        "which name other leaves or miss one"
    )


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get(RECORD_ENV) != "1", reason=f"set {RECORD_ENV}=1 to re-record")
def test_record_adapter_wire(tmp_path: Path) -> None:
    written = record_wire_fixtures(WIRE_DIR, tmp_path)
    assert sorted(path.stem for path in written) == sorted(EXPECTED_FIXTURES)
