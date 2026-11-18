"""Corpus v3 loader: v2 byte digests, schema acceptance, kind, ledger-key, order and mask rules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.parity_waivers import PASSED, WAIVED
from tests.contracts import corpus_v3
from tests.contracts.http_corpus import CorpusError

V2_CASE_COUNT = 16
FAMILIES: dict[str, dict[str, str]] = {"probe": {"parity": "proxy", "origin": "python"}}
# The frozen v3 key order (design section 7.5.2), pinned here independently of the loader.
V3_CASE_ORDER = (
    "schema_version",
    "name",
    "kind",
    "family",
    "backend",
    "credential",
    "ledger_key",
    "request",
    "invocation",
    "seed",
    "response",
    "db_delta",
    "side_effects",
    "normalizers",
    "mask",
    "waiver",
    "native_response",
)


def _ordered(fields: Mapping[str, Any]) -> dict[str, Any]:
    rank = {key: index for index, key in enumerate(V3_CASE_ORDER)}
    return dict(sorted(fields.items(), key=lambda item: rank.get(item[0], len(rank))))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_corpus(
    root: Path,
    *,
    version: int,
    cases: list[dict[str, Any]],
    families: Mapping[str, Mapping[str, str]] = FAMILIES,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": version,
        "cases": [f"{case['name']}.json" for case in cases],
        "families": families,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    for case in cases:
        (root / f"{case['name']}.json").write_text(json.dumps(case))
    return root


def _v2_case(name: str = "probe_v2") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "name": name,
        "family": "probe",
        "backend": "up",
        "credential": "none",
        "request": {
            "method": "GET",
            "path": "/api/probe",
            "query": {},
            "headers": {},
            "body": None,
        },
        "response": {"status": 200, "headers": {}, "body": None},
        "mask": [],
    }


def _v3_http(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 3,
        "name": "probe_http",
        "kind": "http",
        "family": "probe",
        "backend": "up",
        "credential": "none",
        "ledger_key": "HTTP GET /api/probe",
        "request": {
            "method": "GET",
            "path": "/api/probe",
            "query": {},
            "headers": {},
            "body": None,
        },
        "response": {"status": 200, "headers": {}, "body": None},
        "mask": [],
    }
    return _ordered({**base, **fields})


def _v3_kind(
    kind: str,
    *,
    name: str,
    ledger_key: str,
    invocation: Mapping[str, Any],
    response: Any,
) -> dict[str, Any]:
    return _ordered(
        {
            "schema_version": 3,
            "name": name,
            "kind": kind,
            "family": "probe",
            "backend": "up",
            "credential": "none",
            "ledger_key": ledger_key,
            "invocation": dict(invocation),
            "response": response,
            "mask": [],
        }
    )


def _mcp(
    *,
    name: str = "probe_mcp",
    ledger_key: str = "MCP HTTP gobby:list_tasks",
    server: str = "gobby",
    tool: str = "list_tasks",
) -> dict[str, Any]:
    return _v3_kind(
        "mcp",
        name=name,
        ledger_key=ledger_key,
        invocation={"server": server, "tool": tool, "arguments": {}},
        response={"status": 200, "headers": {}, "body": None},
    )


def _ws(
    *,
    name: str = "probe_ws",
    ledger_key: str = "WS message ping",
    frames: tuple[str, ...] = ('{"type": "ping"}',),
) -> dict[str, Any]:
    return _v3_kind(
        "ws",
        name=name,
        ledger_key=ledger_key,
        invocation={"frames": list(frames)},
        response=['{"type": "pong"}'],
    )


def _cli(
    *,
    name: str = "probe_cli",
    ledger_key: str = "CLI gobby status",
    argv: tuple[str, ...] = ("status",),
) -> dict[str, Any]:
    return _v3_kind(
        "cli",
        name=name,
        ledger_key=ledger_key,
        invocation={"argv": list(argv), "stdin": None},
        response={"exit": 0, "stdout": "", "stderr": ""},
    )


VALID_CASES: list[dict[str, Any]] = [
    _v3_http(),
    _mcp(name="probe_mcp_http"),
    _mcp(name="probe_mcp_stdio", ledger_key="MCP stdio gobby:list_tasks"),
    _mcp(name="probe_mcp_named", ledger_key="MCP tasks:list_tasks", server="tasks"),
    _ws(),
    _cli(),
]
V3_CASES = corpus_v3.load_cases(corpus_v3.V3_DIR)


@pytest.mark.unit
def test_v2_corpus_bytes_match_the_digests_taken_at_head() -> None:
    digests: dict[str, str] = json.loads((corpus_v3.V2_DIR / "v2_digests.json").read_text())
    manifest = corpus_v3.load_manifest(corpus_v3.V2_DIR)
    assert len(manifest["cases"]) == V2_CASE_COUNT
    assert set(digests) == {*manifest["cases"], "manifest.json", "mask_vector.json"}
    for name, digest in digests.items():
        assert _sha256(corpus_v3.V2_DIR / name) == digest, name


@pytest.mark.unit
def test_loader_reads_the_v2_and_v3_manifests_in_order() -> None:
    assert corpus_v3.load_manifest(corpus_v3.V2_DIR)["schema_version"] == 2
    assert corpus_v3.load_manifest(corpus_v3.V3_DIR)["schema_version"] == 3
    cases = corpus_v3.load_corpus()
    v2_names = [
        name.removesuffix(".json") for name in corpus_v3.load_manifest(corpus_v3.V2_DIR)["cases"]
    ]
    assert [case["name"] for case in cases[:V2_CASE_COUNT]] == v2_names
    assert len(cases) == V2_CASE_COUNT + len(V3_CASES)
    assert all(corpus_v3.case_kind(case) == "http" for case in cases[:V2_CASE_COUNT])


@pytest.mark.unit
@pytest.mark.parametrize("version", [1, 4])
def test_loader_rejects_a_manifest_schema_other_than_2_or_3(tmp_path: Path, version: int) -> None:
    root = _write_corpus(tmp_path, version=version, cases=[])
    with pytest.raises(CorpusError, match="schema_version"):
        corpus_v3.load_manifest(root)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("manifest_version", "file_version"),
    [(2, 1), (2, 4), (3, 1), (3, 2)],
)
def test_loader_rejects_a_case_schema_other_than_its_manifest(
    tmp_path: Path, manifest_version: int, file_version: int
) -> None:
    case = _v3_http() if manifest_version == 3 else _v2_case()
    case["schema_version"] = file_version
    root = _write_corpus(tmp_path, version=manifest_version, cases=[case])
    with pytest.raises(CorpusError, match=f"schema_version {file_version} differs from manifest"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v2_cases_load_as_http_and_carry_no_kind(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=2, cases=[_v2_case()])
    assert corpus_v3.case_kind(corpus_v3.load_cases(root)[0]) == "http"
    with pytest.raises(CorpusError, match="carries no kind"):
        corpus_v3.load_cases(
            _write_corpus(tmp_path / "kinded", version=2, cases=[{**_v2_case(), "kind": "http"}])
        )


@pytest.mark.unit
@pytest.mark.parametrize("case", VALID_CASES, ids=lambda case: case["name"])
def test_each_kind_loads_with_its_ledger_key(tmp_path: Path, case: dict[str, Any]) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[case])
    assert corpus_v3.load_cases(root) == [case]


@pytest.mark.unit
def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[_v3_http(kind="grpc")])
    with pytest.raises(CorpusError, match="unknown kind 'grpc'"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v3_keys_must_follow_the_declared_order(tmp_path: Path) -> None:
    items = list(_v3_http().items())
    items[1], items[2] = items[2], items[1]
    root = _write_corpus(tmp_path, version=3, cases=[dict(items)])
    with pytest.raises(CorpusError, match="key order"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v3_rejects_an_unknown_key(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[{**_v3_http(), "extra": 1}])
    with pytest.raises(CorpusError, match="unknown key 'extra'"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v3_requires_its_required_keys(tmp_path: Path) -> None:
    case = _v3_http()
    del case["mask"]
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="missing required key 'mask'"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        _v3_http(ledger_key="MCP HTTP gobby:list_tasks"),
        _v3_http(ledger_key="HTTP POST /api/probe"),
        _mcp(ledger_key="WS message ping"),
        _ws(ledger_key="CLI gobby status"),
        _cli(ledger_key="HTTP GET /api/probe"),
    ],
    ids=[
        "http-with-mcp-key",
        "http-method-differs",
        "mcp-with-ws-key",
        "ws-with-cli-key",
        "cli-with-http-key",
    ],
)
def test_ledger_key_prefix_must_match_the_kind(tmp_path: Path, case: dict[str, Any]) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="ledger_key"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_request_is_only_for_http_and_invocation_only_for_other_kinds(tmp_path: Path) -> None:
    with_request = _ordered(
        {
            **_mcp(),
            "request": {"method": "GET", "path": "/", "query": {}, "headers": {}, "body": None},
        }
    )
    root = _write_corpus(tmp_path / "request", version=3, cases=[with_request])
    with pytest.raises(CorpusError, match="request is only for kind http"):
        corpus_v3.load_cases(root)
    http_with_invocation = _ordered({**_v3_http(), "invocation": {"server": "gobby", "tool": "t"}})
    root = _write_corpus(tmp_path / "invocation", version=3, cases=[http_with_invocation])
    with pytest.raises(CorpusError, match="invocation is only for kinds mcp, ws and cli"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "match"),
    [
        (_mcp(ledger_key="MCP HTTP gobby:list_tasks", server="tasks"), "invocation"),
        (_mcp(ledger_key="MCP stdio gobby:list_tasks", server="tasks"), "invocation"),
        (_mcp(ledger_key="MCP tasks:list_tasks", server="gobby"), "invocation"),
        (_mcp(ledger_key="MCP tasks:list_tasks", server="tasks", tool="other"), "invocation"),
        (_mcp(ledger_key="MCP HTTP tasks:list_tasks", server="tasks"), "ledger_key"),
        (_ws(frames=('{"type": "pong"}',)), "frame type"),
        (_cli(argv=("sync",)), "argv"),
    ],
    ids=[
        "gobby-http-form-other-server",
        "gobby-stdio-form-other-server",
        "named-form-other-server",
        "named-form-other-tool",
        "transport-form-requires-gobby",
        "ws-frame-type-differs",
        "cli-argv0-differs",
    ],
)
def test_invocation_must_agree_with_the_ledger_key(
    tmp_path: Path, case: dict[str, Any], match: str
) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match=match):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_mcp_invocation_is_required(tmp_path: Path) -> None:
    case = _mcp()
    del case["invocation"]
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="invocation is required"):
        corpus_v3.load_cases(root)


def _delta(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "table": "probe_rows",
        "key": ["id"],
        "columns": "all",
        "normalizers": {},
        "order": "set",
    }
    for name, value in changes.items():
        if value is None:
            entry.pop(name, None)
        else:
            entry[name] = value
    return entry


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (_delta(order=None), "order is required"),
        (_delta(order="sorted"), "order must be"),
        (_delta(order_by=["id"]), "order_by is not allowed with order 'set'"),
        (_delta(order="as_recorded"), "order_by is required with order 'as_recorded'"),
        (_delta(key=[]), "key is required"),
    ],
    ids=[
        "order-absent",
        "order-unknown",
        "set-with-order-by",
        "as-recorded-without-order-by",
        "no-key",
    ],
)
def test_db_delta_order_and_key_must_be_declared(
    tmp_path: Path, entry: dict[str, Any], match: str
) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[_v3_http(db_delta=[entry])])
    with pytest.raises(CorpusError, match=match):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_db_delta_accepts_set_without_order_by_and_as_recorded_with_it(tmp_path: Path) -> None:
    case = _v3_http(
        db_delta=[_delta(), _delta(table="ordered_rows", order="as_recorded", order_by=["seq"])]
    )
    root = _write_corpus(tmp_path, version=3, cases=[case])
    assert corpus_v3.load_cases(root) == [case]


@pytest.mark.unit
def test_v3_mask_pointer_matching_nothing_is_a_loader_error(tmp_path: Path) -> None:
    root = _write_corpus(
        tmp_path / "missing", version=3, cases=[_v3_http(mask=["/response/body/missing"])]
    )
    with pytest.raises(CorpusError, match="matches no recorded field"):
        corpus_v3.load_cases(root)
    root = _write_corpus(
        tmp_path / "present", version=3, cases=[_v3_http(mask=["/response/status"])]
    )
    assert len(corpus_v3.load_cases(root)) == 1


@pytest.mark.unit
def test_v2_mask_pointer_matching_nothing_stays_a_no_op(tmp_path: Path) -> None:
    case = _v2_case()
    case["mask"] = ["/response/body/missing"]
    root = _write_corpus(tmp_path, version=2, cases=[case])
    assert corpus_v3.load_cases(root) == [case]


@pytest.mark.unit
def test_normalizer_names_are_the_closed_field_set(tmp_path: Path) -> None:
    root = _write_corpus(
        tmp_path, version=3, cases=[_v3_http(normalizers={"/response/status": "set"})]
    )
    with pytest.raises(CorpusError, match="not a field normalizer"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v3_manifest_lists_its_cases_once() -> None:
    manifest = corpus_v3.load_manifest(corpus_v3.V3_DIR)
    names = [case["name"] for case in V3_CASES]
    assert len(V3_CASES) == len(manifest["cases"])
    assert len(set(names)) == len(names)


def _self_replay_outcome(case: Mapping[str, Any]) -> str:
    """Replay one schema 3 case against the daemon and return its classification.

    The schema 3 senders arrive with the recorder (C8). Until then this raises, so no case
    can pass without being replayed.
    """
    raise NotImplementedError(
        f"{case['name']} ({corpus_v3.case_kind(case)}): schema 3 senders arrive with the recorder"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("case", V3_CASES, ids=lambda case: case["name"])
def test_v3_proxy_self_replay(case: dict[str, Any]) -> None:
    assert _self_replay_outcome(case) in (PASSED, WAIVED), case["name"]
