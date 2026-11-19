"""Waiver matching for the four case kinds: the HTTP template rule and exact ledger keys.

The vector file is shared with the Rust matcher (design section 7.7.4), so its shape is pinned
here. Cited cases run against fixture manifests written into a temporary directory; the
committed waiver manifest is never read or changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import parity_waivers
from scripts.parity_waivers import DIFF, WAIVED, Waiver, WaiverError, load_waivers

VECTORS_PATH = Path(__file__).resolve().parent / "waiver_match_vectors.json"
VECTOR_KEYS = frozenset({"schema_version", "http", "keyed"})
HTTP_ROW_KEYS = frozenset({"surface", "method", "path", "expect"})
KEYED_ROW_KEYS = frozenset({"surface", "key", "kind", "expect"})
WAIVER_ID = "parity.fixture-case.surface-match"
HTTP_PATCH = "HTTP PATCH /api/cron/jobs/{job_id}"
MCP_HTTP = "MCP HTTP gobby:fixture_read"
MCP_STDIO = "MCP stdio gobby:fixture_read"
WS_EVENT = "WS message fixture_event"
WS_OTHER = "WS message fixture_other"
CLI_LIST = "CLI gobby fixture list"
CLI_PARENT = "CLI gobby fixture"

_VECTORS: dict[str, Any] = json.loads(VECTORS_PATH.read_text())
HTTP_VECTORS: list[dict[str, Any]] = _VECTORS["http"]
KEYED_VECTORS: list[dict[str, Any]] = _VECTORS["keyed"]

# (kind, waiver surface, case key, method, path) for one cited case of each kind.
CITED = [
    pytest.param("http", HTTP_PATCH, "", "PATCH", "/api/cron/jobs/job-1", id="http"),
    pytest.param("mcp", MCP_HTTP, MCP_HTTP, "", "", id="mcp-http"),
    pytest.param("mcp", MCP_STDIO, MCP_STDIO, "", "", id="mcp-stdio"),
    pytest.param("ws", WS_EVENT, WS_EVENT, "", "", id="ws"),
    pytest.param("cli", CLI_LIST, CLI_LIST, "", "", id="cli"),
]

# (kind, waiver surface, case key) where the case names a different surface of the same kind.
MISMATCHED = [
    pytest.param("mcp", MCP_STDIO, MCP_HTTP, id="mcp-form"),
    pytest.param("ws", WS_EVENT, WS_OTHER, id="ws-type"),
    pytest.param("cli", CLI_LIST, CLI_PARENT, id="cli-prefix"),
]


def _write_manifest(tmp_path: Path, entries: dict[str, str]) -> Path:
    """Write a fixture waiver manifest mapping each waiver id to its surface."""
    waivers = [
        {
            "id": waiver_id,
            "surface": surface,
            "behaviour": "fixture divergence for the surface matcher",
            "decision": "#25938",
            "replacement": "fixture replacement for the surface matcher",
        }
        for waiver_id, surface in entries.items()
    ]
    path = tmp_path / "parity-waivers.json"
    path.write_text(json.dumps({"schema_version": 1, "waivers": waivers}))
    return path


def _load(tmp_path: Path, entries: dict[str, str]) -> dict[str, Waiver]:
    surfaces = frozenset(entries.values())
    return load_waivers(_write_manifest(tmp_path, entries), surfaces=surfaces)


def _cited_outcome(
    waivers: dict[str, Waiver],
    kind: str,
    key: str,
    method: str,
    path: str,
    native: dict[str, object],
    actual: dict[str, object],
) -> str:
    """Restate the cited rule of design section 7.8 over the matcher: only the native answer waives."""
    parity_waivers.cite_waiver(waivers, WAIVER_ID, kind=kind, key=key, method=method, path=path)
    return WAIVED if actual == native else DIFF


def test_vector_file_pins_the_shared_shape() -> None:
    assert set(_VECTORS) == VECTOR_KEYS
    assert _VECTORS["schema_version"] == 1
    assert all(set(row) == HTTP_ROW_KEYS for row in HTTP_VECTORS)
    assert all(set(row) == KEYED_ROW_KEYS for row in KEYED_VECTORS)
    assert {row["kind"] for row in KEYED_VECTORS} == {"mcp", "ws", "cli"}
    assert all(type(row["expect"]) is bool for row in HTTP_VECTORS + KEYED_VECTORS)


@pytest.mark.parametrize("row", HTTP_VECTORS)
def test_http_vectors_keep_the_template_rule(row: dict[str, Any]) -> None:
    matched = parity_waivers.surface_matches(row["surface"], row["method"], row["path"])
    assert matched is row["expect"]


@pytest.mark.parametrize("row", KEYED_VECTORS)
def test_keyed_vectors_require_the_exact_ledger_key(row: dict[str, Any]) -> None:
    matched = parity_waivers.surface_matches(row["surface"], kind=row["kind"], key=row["key"])
    assert matched is row["expect"]


def test_http_kind_ignores_the_key_and_keeps_positional_calls() -> None:
    assert parity_waivers.surface_matches(HTTP_PATCH, "PATCH", "/api/cron/jobs/job-1") is True
    matched = parity_waivers.surface_matches(
        HTTP_PATCH, "PATCH", "/api/cron/jobs/job-1", kind="http", key=MCP_HTTP
    )
    assert matched is True


def test_unknown_kind_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="grpc"):
        parity_waivers.surface_matches(CLI_LIST, kind="grpc", key=CLI_LIST)


@pytest.mark.parametrize("key", [MCP_HTTP, WS_EVENT, CLI_LIST])
def test_keyed_call_without_kind_raises(key: str) -> None:
    with pytest.raises(WaiverError, match="needs kind="):
        parity_waivers.surface_matches(key, key=key)


@pytest.mark.parametrize(("kind", "surface", "key", "method", "path"), CITED)
def test_cited_case_resolves_to_its_own_waiver(
    tmp_path: Path, kind: str, surface: str, key: str, method: str, path: str
) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: surface})
    cited = parity_waivers.cite_waiver(
        waivers, WAIVER_ID, kind=kind, key=key, method=method, path=path
    )
    assert cited is waivers[WAIVER_ID]


@pytest.mark.parametrize(("kind", "surface", "key", "method", "path"), CITED)
def test_cited_case_is_waived_only_on_its_native_answer(
    tmp_path: Path, kind: str, surface: str, key: str, method: str, path: str
) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: surface})
    native: dict[str, object] = {"status": 200, "body": "native"}
    recorded: dict[str, object] = {"status": 200, "body": "python"}
    other: dict[str, object] = {"status": 500, "body": "other"}
    args = (waivers, kind, key, method, path)
    assert _cited_outcome(*args, native=native, actual=native) == WAIVED
    assert _cited_outcome(*args, native=native, actual=recorded) == DIFF
    assert _cited_outcome(*args, native=native, actual=other) == DIFF


@pytest.mark.parametrize(("kind", "surface", "key"), MISMATCHED)
def test_keyed_case_naming_another_surface_raises(
    tmp_path: Path, kind: str, surface: str, key: str
) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: surface})
    with pytest.raises(WaiverError, match="is not waiver"):
        parity_waivers.cite_waiver(waivers, WAIVER_ID, kind=kind, key=key)


def test_http_case_citing_a_keyed_waiver_raises(tmp_path: Path) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: MCP_HTTP})
    with pytest.raises(WaiverError, match="is not waiver"):
        parity_waivers.cite_waiver(
            waivers, WAIVER_ID, kind="http", method="PATCH", path="/api/cron/jobs/job-1"
        )


def test_keyed_case_citing_an_http_waiver_raises(tmp_path: Path) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: HTTP_PATCH})
    with pytest.raises(WaiverError, match="is not waiver"):
        parity_waivers.cite_waiver(waivers, WAIVER_ID, kind="cli", key=CLI_LIST)


def test_unknown_waiver_id_raises(tmp_path: Path) -> None:
    waivers = _load(tmp_path, {WAIVER_ID: CLI_LIST})
    with pytest.raises(WaiverError, match="unknown waiver"):
        parity_waivers.cite_waiver(
            waivers, "parity.missing.surface-match", kind="cli", key=CLI_LIST
        )


def test_one_manifest_holds_all_four_kinds(tmp_path: Path) -> None:
    entries = {
        "parity.http-case.surface-match": HTTP_PATCH,
        "parity.mcp-case.surface-match": MCP_HTTP,
        "parity.ws-case.surface-match": WS_EVENT,
        "parity.cli-case.surface-match": CLI_LIST,
    }
    waivers = _load(tmp_path, entries)
    cited = {
        "parity.http-case.surface-match": parity_waivers.cite_waiver(
            waivers,
            "parity.http-case.surface-match",
            kind="http",
            method="PATCH",
            path="/api/cron/jobs/job-1",
        ),
        "parity.mcp-case.surface-match": parity_waivers.cite_waiver(
            waivers, "parity.mcp-case.surface-match", kind="mcp", key=MCP_HTTP
        ),
        "parity.ws-case.surface-match": parity_waivers.cite_waiver(
            waivers, "parity.ws-case.surface-match", kind="ws", key=WS_EVENT
        ),
        "parity.cli-case.surface-match": parity_waivers.cite_waiver(
            waivers, "parity.cli-case.surface-match", kind="cli", key=CLI_LIST
        ),
    }
    assert {waiver_id: waiver.surface for waiver_id, waiver in cited.items()} == entries
