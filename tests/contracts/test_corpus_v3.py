"""Corpus v3 loader: v2 byte digests, schema acceptance, kind, ledger-key, order and mask rules."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.parity_waivers import DIFF, PASSED, WAIVED, Waiver
from tests.contracts import corpus_v3, http_corpus, recorder_v3
from tests.contracts.http_corpus import CorpusError, classify_replay
from tests.contracts.recorder_daemon_v3 import recorder_daemon

V2_CASE_COUNT = 16
# The schema 3 case files in manifest order. Adding or dropping a case changes this pin in the same
# change, as V2_CASE_COUNT does for schema 2, so a case cannot leave the corpus silently.
V3_CASE_FILES = (
    "config_values_patch.json",
    "mcp_http_gobby_list_tools.json",
    "mcp_stdio_gobby_list_tools.json",
    "ws_terminal_list.json",
    "cli_schema_help.json",
)
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
    transport = "stdio" if ledger_key.startswith("MCP stdio ") else "http"
    return _v3_kind(
        "mcp",
        name=name,
        ledger_key=ledger_key,
        invocation={"transport": transport, "server": server, "tool": tool, "arguments": {}},
        response={"jsonrpc": "2.0", "id": 2, "result": {}},
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


# C38: the ledger and waiver manifest is frozen in this chunk (protocol line 17, design section 13.2).
# Each digest is the SHA-256 of that file at the worktree HEAD when this guard was written.
LEDGER_DIGESTS: dict[str, str] = {
    "docs/contracts/parity-waivers.json": (
        "1a53dacac2848b1e0f8989f876d8f7b27c1afc641c6fb7fc2348f15d24551c6a"
    ),
    "docs/contracts/parity-ledger.md": (
        "80ca3158a414ae23e95dee02fe9df6909d5c140cbbbcd95e3d68c029d3173a37"
    ),
}


def _ledger_mismatches(root: Path) -> list[str]:
    return [name for name, digest in LEDGER_DIGESTS.items() if _sha256(root / name) != digest]


@pytest.mark.unit
def test_ledger_and_waiver_manifest_match_the_digests_taken_at_head() -> None:
    assert _ledger_mismatches(recorder_v3.REPO_ROOT) == [], (
        "the ledger and waiver manifest must not change in a chunk landing (design section 13.2)"
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(LEDGER_DIGESTS))
def test_ledger_guard_reports_a_copy_with_one_flipped_byte(tmp_path: Path, name: str) -> None:
    for other in LEDGER_DIGESTS:
        copy = tmp_path / other
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes((recorder_v3.REPO_ROOT / other).read_bytes())
    flipped = bytearray((tmp_path / name).read_bytes())
    flipped[-1] ^= 0x01
    (tmp_path / name).write_bytes(bytes(flipped))
    assert _ledger_mismatches(tmp_path) == [name]


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


def _with_transport(case: dict[str, Any], transport: str) -> dict[str, Any]:
    return {**case, "invocation": {**case["invocation"], "transport": transport}}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ledger_key", "transport"),
    [("MCP HTTP gobby:list_tasks", "stdio"), ("MCP stdio gobby:list_tasks", "http")],
    ids=["http-key-stdio-invocation", "stdio-key-http-invocation"],
)
def test_keyed_mcp_transport_must_match_the_invocation(
    tmp_path: Path, ledger_key: str, transport: str
) -> None:
    case = _with_transport(_mcp(ledger_key=ledger_key), transport)
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="names transport"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ledger_key", "server", "transport"),
    [
        ("MCP HTTP gobby:list_tasks", "gobby", "http"),
        ("MCP stdio gobby:list_tasks", "gobby", "stdio"),
        ("MCP tasks:list_tasks", "tasks", "http"),
        ("MCP tasks:list_tasks", "tasks", "stdio"),
    ],
)
def test_mcp_transport_is_checked_only_when_the_key_names_one(
    tmp_path: Path, ledger_key: str, server: str, transport: str
) -> None:
    case = _with_transport(_mcp(ledger_key=ledger_key, server=server), transport)
    root = _write_corpus(tmp_path, version=3, cases=[case])
    assert corpus_v3.load_cases(root) == [case]


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


NATIVE_WAIVER = "wv-probe-native.native-extension"
BEHAVIOUR_WAIVER = "wv-probe-behaviour.behaviour"


def _without_response(case: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in case.items() if key != "response"}


@pytest.mark.unit
def test_response_is_required_unless_the_waiver_is_native_extension(tmp_path: Path) -> None:
    case = _without_response(_v3_http(waiver=BEHAVIOUR_WAIVER, native_response={"status": 200}))
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="missing required key 'response'"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_native_extension_case_loads_without_a_python_response(tmp_path: Path) -> None:
    case = _without_response(_v3_http(waiver=NATIVE_WAIVER, native_response={"status": 200}))
    root = _write_corpus(tmp_path, version=3, cases=[case])
    assert [loaded["name"] for loaded in corpus_v3.load_cases(root)] == ["probe_http"]


@pytest.mark.unit
def test_native_extension_case_cannot_carry_a_python_response(tmp_path: Path) -> None:
    case = _v3_http(waiver=NATIVE_WAIVER, native_response={"status": 200})
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match="native-extension waiver cannot carry a response"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_timestamp_normalizer_may_declare_an_after_column(tmp_path: Path) -> None:
    entry = {
        "table": "probe_rows",
        "key": ["id"],
        "columns": ["id", "created_at", "updated_at"],
        "normalizers": {"updated_at": {"name": "timestamp", "after": "created_at"}},
        "order": "set",
    }
    root = _write_corpus(tmp_path, version=3, cases=[_v3_http(db_delta=[entry])])
    assert corpus_v3.load_cases(root)[0]["db_delta"] == [entry]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "match"),
    [
        (
            _v3_http(
                db_delta=[
                    {
                        "table": "probe_rows",
                        "key": ["id"],
                        "columns": "all",
                        "normalizers": {"id": {"name": "uuid_ordinal", "after": "x"}},
                        "order": "set",
                    }
                ]
            ),
            "only the timestamp normalizer takes 'after'",
        ),
        (
            _v3_http(normalizers={"/response/body/at": {"name": "timestamp", "after": "x"}}),
            "'after' is declared only on db_delta normalizers",
        ),
        (
            _v3_http(normalizers={"response/body/at": "timestamp"}),
            "must be a JSON pointer",
        ),
    ],
    ids=["after-on-other-name", "after-outside-db-delta", "pointer-key-required"],
)
def test_after_and_pointer_keys_are_checked(
    tmp_path: Path, case: dict[str, Any], match: str
) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[case])
    with pytest.raises(CorpusError, match=match):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_v3_manifest_lists_its_cases_once() -> None:
    manifest = corpus_v3.load_manifest(corpus_v3.V3_DIR)
    names = [case["name"] for case in V3_CASES]
    assert manifest["cases"] == list(V3_CASE_FILES)
    assert names == [file_name.removesuffix(".json") for file_name in V3_CASE_FILES]
    assert len(set(names)) == len(names)


@pytest.mark.unit
def test_v3_directory_holds_the_pinned_cases_and_one_vector() -> None:
    files = {path.name for path in corpus_v3.V3_DIR.glob("*.json")}
    assert files == {*V3_CASE_FILES, "manifest.json", "grant_encoding.json"}


@pytest.mark.unit
def test_loader_refuses_a_v3_case_file_the_manifest_does_not_list(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[_v3_http(name="probe_listed")])
    (root / "probe_unlisted.json").write_text(json.dumps(_v3_http(name="probe_unlisted")))
    with pytest.raises(CorpusError, match=r"probe_unlisted\.json: .*not listed in manifest\.json"):
        corpus_v3.load_cases(root)


@pytest.mark.unit
def test_loader_accepts_a_declared_non_case_vector_beside_the_cases(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=3, cases=[_v3_http()])
    (root / "grant_encoding.json").write_text(json.dumps({"vector": []}))
    assert len(corpus_v3.load_cases(root)) == 1


@pytest.mark.unit
def test_v2_directory_keeps_its_unlisted_json_files(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, version=2, cases=[_v2_case()])
    (root / "mask_vector.json").write_text(json.dumps({}))
    assert len(corpus_v3.load_cases(root)) == 1


@pytest.fixture(scope="module")
def v3_self_replay() -> Iterator[dict[str, Any]]:
    """Replay every schema 3 case, in manifest order, against one isolated Python daemon.

    Each case's seed resets the state it reads, so one daemon serves the whole manifest. A case
    that cannot run is stored as its exception and re-raised only by that case's own test.
    """
    waivers = http_corpus.load_replay_waivers()
    with tempfile.TemporaryDirectory(prefix="v3-self-replay-") as scratch:
        with (
            recorder_v3.open_run_database() as run,
            recorder_daemon(base_url=run.base_url, database_url=run.url) as daemon,
        ):
            operator = daemon.operator_headers()
            outcomes: dict[str, Any] = {}
            for case in V3_CASES:
                try:
                    outcomes[case["name"]] = recorder_v3.replay_case(
                        run, daemon, case, waivers, operator=operator, scratch=Path(scratch)
                    )
                except Exception as error:
                    outcomes[case["name"]] = error
            yield outcomes


@pytest.mark.e2e
@pytest.mark.parametrize("case", V3_CASES, ids=lambda case: case["name"])
def test_v3_proxy_self_replay(case: dict[str, Any], v3_self_replay: dict[str, Any]) -> None:
    outcome = v3_self_replay[case["name"]]
    if isinstance(outcome, BaseException):
        raise outcome
    assert outcome.outcome in (PASSED, WAIVED), case["name"]


# Synthetic in-test recordings (C23): no v3 case files are needed. Each recording holds one
# db_delta entry and one pg_notify side effect, and a replay returns the recorded values.
RECORDED_DB_DELTA: dict[str, Any] = {
    "added": [{"id": 2, "state": "new"}],
    "removed": [{"id": 3, "state": "old"}],
    "changed": [{"key": {"id": 1}, "column": "state", "before": "new", "after": "done"}],
}
RECORDED_CAPTURES: list[dict[str, Any]] = [
    {"channel": "gobby_config_changed", "payload": {"key": "probe.setting"}}
]
WAIVER_ID = "parity.probe.surface-match"
SELF_REPLAY_BUILDERS = [
    pytest.param(_v3_http, id="http"),
    pytest.param(_mcp, id="mcp"),
    pytest.param(_ws, id="ws"),
    pytest.param(_cli, id="cli"),
]
CITED_BUILDERS = [
    pytest.param(_v3_http, "HTTP GET /api/probe", id="http"),
    pytest.param(_mcp, "MCP HTTP gobby:list_tasks", id="mcp"),
    pytest.param(_ws, "WS message ping", id="ws"),
    pytest.param(_cli, "CLI gobby status", id="cli"),
]


def _changed_db_column(observed: dict[str, Any]) -> dict[str, Any]:
    observed["db_delta"][0]["changed"][0]["after"] = "failed"
    return observed


def _removed_db_row(observed: dict[str, Any]) -> dict[str, Any]:
    observed["db_delta"][0]["removed"] = []
    return observed


def _dropped_pg_notify(observed: dict[str, Any]) -> dict[str, Any]:
    observed["side_effects"][0] = []
    return observed


def _changed_side_effect(observed: dict[str, Any]) -> dict[str, Any]:
    observed["side_effects"][0][0]["payload"]["key"] = "other.setting"
    return observed


MUTATIONS = [
    pytest.param(_changed_db_column, id="changed-db-column"),
    pytest.param(_removed_db_row, id="removed-db-row"),
    pytest.param(_dropped_pg_notify, id="dropped-pg-notify"),
    pytest.param(_changed_side_effect, id="changed-side-effect"),
]


def _with_recording(case: dict[str, Any]) -> dict[str, Any]:
    """Give a case one db_delta entry and one pg_notify side effect, each with its recording."""
    case["db_delta"] = [
        {**_delta(columns=["id", "state"]), "recorded": copy.deepcopy(RECORDED_DB_DELTA)}
    ]
    case["side_effects"] = [
        {
            "channel": "pg_notify",
            "match": {"channels": ["gobby_config_changed"]},
            "normalizers": {},
            "recorded": copy.deepcopy(RECORDED_CAPTURES),
        }
    ]
    return _ordered(case)


def _replayed(case: Mapping[str, Any]) -> dict[str, Any]:
    """The observation an unchanged replay of the recording returns, in entry order."""
    return {
        "db_delta": [copy.deepcopy(entry["recorded"]) for entry in case["db_delta"]],
        "side_effects": [copy.deepcopy(entry["recorded"]) for entry in case["side_effects"]],
    }


def _cited(
    build: Callable[[], dict[str, Any]], surface: str
) -> tuple[dict[str, Any], dict[str, Waiver]]:
    case = _with_recording(build())
    case["waiver"] = WAIVER_ID
    case["native_response"] = copy.deepcopy(case["response"])
    waiver = Waiver(
        id=WAIVER_ID, surface=surface, behaviour="probe", decision="#25938", replacement="probe"
    )
    return _ordered(case), {WAIVER_ID: waiver}


@pytest.mark.unit
@pytest.mark.parametrize("build", SELF_REPLAY_BUILDERS)
def test_unchanged_recording_passes_on_self_replay(build: Callable[[], dict[str, Any]]) -> None:
    case = _with_recording(build())
    result = classify_replay(case, case["response"], {}, observed=_replayed(case))
    assert result.outcome == PASSED


@pytest.mark.unit
@pytest.mark.parametrize("build", SELF_REPLAY_BUILDERS)
@pytest.mark.parametrize("mutate", MUTATIONS)
def test_changed_recording_diffs_on_self_replay(
    build: Callable[[], dict[str, Any]], mutate: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    case = _with_recording(build())
    result = classify_replay(case, case["response"], {}, observed=mutate(_replayed(case)))
    assert result.outcome == DIFF


@pytest.mark.unit
def test_diff_names_the_first_differing_json_pointer() -> None:
    case = _with_recording(_v3_http())
    observed = _changed_db_column(_replayed(case))
    result = classify_replay(case, case["response"], {}, observed=observed)
    assert result.outcome == DIFF
    assert result.pointer == "/db_delta/0/recorded/changed/0/after"


@pytest.mark.unit
@pytest.mark.parametrize(("build", "surface"), CITED_BUILDERS)
def test_unchanged_recording_is_waived_on_cited_classify(
    build: Callable[[], dict[str, Any]], surface: str
) -> None:
    case, waivers = _cited(build, surface)
    result = classify_replay(case, case["native_response"], waivers, observed=_replayed(case))
    assert result.outcome == WAIVED


@pytest.mark.unit
@pytest.mark.parametrize(("build", "surface"), CITED_BUILDERS)
@pytest.mark.parametrize("mutate", MUTATIONS)
def test_changed_recording_diffs_on_cited_classify(
    build: Callable[[], dict[str, Any]],
    surface: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    case, waivers = _cited(build, surface)
    observed = mutate(_replayed(case))
    result = classify_replay(case, case["native_response"], waivers, observed=observed)
    assert result.outcome == DIFF
