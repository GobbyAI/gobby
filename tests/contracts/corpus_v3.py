"""Loader for the HTTP contract corpus across schema 2 and schema 3.

Schema 2 cases (``tests/contracts/http/``) are all kind ``http`` and carry no ``kind``.
Schema 3 cases (``tests/contracts/v3/``) name their kind and the exact ledger key they cite.
This module validates files only. It sends no request and classifies nothing; replay and
recording live in the replay and recorder modules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.contracts.http_corpus import (
    BACKEND_VALUES,
    CORPUS_DIR,
    CREDENTIALS,
    MANIFEST_NAME,
    ORIGIN_VALUES,
    PARITY_VALUES,
    SCHEMA_VERSIONS,
    CorpusError,
)

V2_DIR = CORPUS_DIR
V3_DIR = Path(__file__).resolve().parent / "v3"
# JSON vectors kept beside the schema 3 cases. Tests read them; the loader never replays them.
V3_NON_CASE_FILES = frozenset({"grant_encoding.json"})
KINDS = frozenset({"http", "mcp", "ws", "cli"})
CHANNELS = frozenset({"pg_notify", "ws_publish", "files_home_write"})
FIELD_NORMALIZERS = frozenset({"uuid_ordinal", "monotonic", "timestamp", "bytes", "none"})
ORDER_MODES = frozenset({"set", "as_recorded"})
MCP_TRANSPORTS = frozenset({"http", "stdio"})
# Design section 7.5.2: keys appear in this order; absent keys are omitted.
V3_KEY_ORDER = (
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
# ``response`` is required unless the cited waiver is a native-extension (H44; see _check_response_presence).
V3_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "kind",
        "family",
        "backend",
        "credential",
        "ledger_key",
        "mask",
    }
)
_KEY_PATTERNS = {
    "http": re.compile(r"HTTP (?P<method>[A-Z]+) (?P<template>/\S*)"),
    "mcp": re.compile(r"MCP (?:(?P<transport>HTTP|stdio) )?(?P<server>[^\s:]+):(?P<tool>\S+)"),
    "ws": re.compile(r"WS message (?P<type>\S+)"),
    "cli": re.compile(r"CLI gobby (?P<verb>\S+)"),
}


def load_manifest(corpus_dir: Path) -> dict[str, Any]:
    """Read one manifest, accepting schema versions 2 and 3 only."""
    manifest = _read_object(corpus_dir / MANIFEST_NAME)
    version = manifest.get("schema_version")
    if not isinstance(version, int) or version not in SCHEMA_VERSIONS:
        raise CorpusError(
            f"{corpus_dir.name}/{MANIFEST_NAME}: schema_version {version!r} is neither 2 nor 3"
        )
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not all(
        isinstance(name, str) and name.endswith(".json") for name in cases
    ):
        raise CorpusError(f"{corpus_dir.name}/{MANIFEST_NAME}: cases must be .json file names")
    families = manifest.get("families")
    if not isinstance(families, dict):
        raise CorpusError(f"{corpus_dir.name}/{MANIFEST_NAME}: families must be an object")
    for name, family in families.items():
        if (
            not isinstance(family, dict)
            or family.get("parity") not in PARITY_VALUES
            or family.get("origin") not in ORIGIN_VALUES
        ):
            raise CorpusError(f"family {name!r} needs a parity and an origin")
    return manifest


def load_cases(corpus_dir: Path) -> list[dict[str, Any]]:
    """Load and validate one manifest's cases, in manifest order.

    A schema 3 directory must list every JSON case file in it: an unlisted file is refused rather
    than silently left out of the replay.
    """
    manifest = load_manifest(corpus_dir)
    version = manifest["schema_version"]
    families: Mapping[str, Any] = manifest["families"]
    if version == 3:
        _check_listed_files(corpus_dir, manifest["cases"])
    cases: list[dict[str, Any]] = []
    for file_name in manifest["cases"]:
        case = _read_object(corpus_dir / file_name)
        _validate_case(case, file_name=file_name, version=version, families=families)
        cases.append(case)
    return cases


def _check_listed_files(corpus_dir: Path, listed: list[str]) -> None:
    """Refuse a schema 3 JSON file that is neither listed, the manifest, nor a declared vector."""
    for path in sorted(corpus_dir.glob("*.json")):
        if path.name == MANIFEST_NAME or path.name in listed or path.name in V3_NON_CASE_FILES:
            continue
        raise CorpusError(
            f"{path.name}: a .json file in this corpus is not listed in {MANIFEST_NAME} "
            "and is not a declared non-case vector"
        )


def load_corpus() -> list[dict[str, Any]]:
    """Every case in replay order: the schema 2 manifest's cases, then the schema 3 manifest's."""
    return [*load_cases(V2_DIR), *load_cases(V3_DIR)]


def case_kind(case: Mapping[str, Any]) -> str:
    """A case's kind. Schema 2 cases carry no kind and are kind ``http``."""
    return str(case.get("kind", "http"))


def _read_object(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise CorpusError(f"{path.name} must be a JSON object")
    return document


def _validate_case(
    case: Mapping[str, Any], *, file_name: str, version: int, families: Mapping[str, Any]
) -> None:
    if case.get("schema_version") != version:
        raise CorpusError(
            f"{file_name}: schema_version {case.get('schema_version')!r} "
            f"differs from manifest {version!r}"
        )
    if case.get("name") != file_name.removesuffix(".json"):
        raise CorpusError(f"{file_name}: name {case.get('name')!r} must equal the file stem")
    family = case.get("family")
    if not isinstance(family, str) or family not in families:
        raise CorpusError(f"{file_name}: family {family!r} is not in the manifest")
    if case.get("backend") not in BACKEND_VALUES:
        raise CorpusError(
            f"{file_name}: backend {case.get('backend')!r} is neither 'up' nor 'down'"
        )
    if case.get("credential") not in CREDENTIALS:
        raise CorpusError(f"{file_name}: credential {case.get('credential')!r} is unknown")
    if version == 2:
        _validate_schema_2(case, file_name)
    else:
        _validate_schema_3(case, file_name)


def _validate_schema_2(case: Mapping[str, Any], file_name: str) -> None:
    if "kind" in case:
        raise CorpusError(f"{file_name}: a schema 2 case carries no kind")
    _check_request(case, file_name)
    _check_response(case["response"], "http", file_name)
    _check_mask_shape(case, file_name)


def _validate_schema_3(case: Mapping[str, Any], file_name: str) -> None:
    unknown = [key for key in case if key not in V3_KEY_ORDER]
    if unknown:
        raise CorpusError(f"{file_name}: unknown key {unknown[0]!r}")
    missing = sorted(V3_REQUIRED_KEYS - set(case))
    if missing:
        raise CorpusError(f"{file_name}: missing required key {missing[0]!r}")
    positions = [V3_KEY_ORDER.index(key) for key in case]
    if positions != sorted(positions):
        raise CorpusError(f"{file_name}: keys are out of key order")
    kind = case["kind"]
    if kind not in KINDS:
        raise CorpusError(f"{file_name}: unknown kind {kind!r}")
    _check_response_presence(case, file_name)
    if kind == "http":
        if "request" not in case:
            raise CorpusError(f"{file_name}: request is required for kind http")
        if "invocation" in case:
            raise CorpusError(f"{file_name}: invocation is only for kinds mcp, ws and cli")
        _check_request(case, file_name)
    else:
        if "request" in case:
            raise CorpusError(f"{file_name}: request is only for kind http")
        if "invocation" not in case:
            raise CorpusError(f"{file_name}: invocation is required for kind {kind}")
    key_match = _check_ledger_key(case, kind, file_name)
    if kind != "http":
        _check_invocation(case, kind, key_match, file_name)
    if "response" in case:
        _check_response(case["response"], kind, file_name)
    _check_mask_shape(case, file_name)
    for name in ("seed", "db_delta", "side_effects", "normalizers", "waiver"):
        if name in case:
            _check_optional(case, name, file_name)
    for entry in case.get("db_delta", []):
        _check_db_delta(entry, file_name)
    for effect in case.get("side_effects", []):
        _check_side_effect(effect, file_name)
    _check_mask_resolves(case, file_name)


def _is_native_extension(case: Mapping[str, Any]) -> bool:
    """A waiver id ends in ``.native-extension`` exactly as ``Waiver.native_extension`` reads it."""
    waiver = case.get("waiver")
    return isinstance(waiver, str) and waiver.endswith(".native-extension")


def _check_response_presence(case: Mapping[str, Any], file_name: str) -> None:
    """H44: a native-extension citation has no Python response; every other case has one."""
    if _is_native_extension(case):
        if "response" in case:
            raise CorpusError(f"{file_name}: a native-extension waiver cannot carry a response")
    elif "response" not in case:
        raise CorpusError(
            f"{file_name}: missing required key 'response' (only a native-extension omits it)"
        )


def _check_optional(case: Mapping[str, Any], name: str, file_name: str) -> None:
    value = case[name]
    if name == "waiver":
        if not isinstance(value, str):
            raise CorpusError(f"{file_name}: waiver must be a waiver id")
    elif name == "normalizers":
        _check_normalizers(value, file_name)
        _check_pointer_keys(value, file_name)
    elif name == "seed":
        if not isinstance(value, list) or not all(_is_seed_step(step) for step in value):
            raise CorpusError(f"{file_name}: each seed step is one http or sql object")
    elif not isinstance(value, list):
        raise CorpusError(f"{file_name}: {name} must be a list")


def _is_seed_step(step: Any) -> bool:
    return isinstance(step, dict) and len(step) == 1 and next(iter(step)) in {"http", "sql"}


def _check_request(case: Mapping[str, Any], file_name: str) -> None:
    request = case["request"]
    if (
        not isinstance(request, dict)
        or not isinstance(request.get("method"), str)
        or not isinstance(request.get("path"), str)
    ):
        raise CorpusError(f"{file_name}: request needs a method and a path")


def _check_response(response: Any, kind: str, file_name: str) -> None:
    if kind == "ws":
        valid = isinstance(response, list) and all(isinstance(frame, str) for frame in response)
    elif kind == "cli":
        valid = (
            isinstance(response, dict)
            and isinstance(response.get("exit"), int)
            and isinstance(response.get("stdout"), str)
            and isinstance(response.get("stderr"), str)
        )
    elif kind == "mcp":
        # Both transports record the JSON-RPC message for the tools/call, result or error.
        valid = (
            isinstance(response, dict)
            and response.get("jsonrpc") == "2.0"
            and isinstance(response.get("id"), int)
            and ("result" in response) != ("error" in response)
        )
    else:
        valid = isinstance(response, dict) and isinstance(response.get("status"), int)
    if not valid:
        raise CorpusError(f"{file_name}: response has the wrong shape for kind {kind}")


def _check_mask_shape(case: Mapping[str, Any], file_name: str) -> None:
    mask = case["mask"]
    if not isinstance(mask, list) or not all(isinstance(pointer, str) for pointer in mask):
        raise CorpusError(f"{file_name}: mask must be a list of pointers")


def _check_mask_resolves(case: Mapping[str, Any], file_name: str) -> None:
    for pointer in case["mask"]:
        if not pointer.startswith("/"):
            raise CorpusError(f"{file_name}: mask pointer {pointer!r} must start with '/'")
        if not _resolves(case, pointer):
            raise CorpusError(f"{file_name}: mask {pointer!r} matches no recorded field")


def _resolves(document: Any, pointer: str) -> bool:
    node: Any = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return False
    return True


def _check_ledger_key(case: Mapping[str, Any], kind: str, file_name: str) -> dict[str, str | None]:
    key = case["ledger_key"]
    match = _KEY_PATTERNS[kind].fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise CorpusError(f"{file_name}: ledger_key {key!r} is not a {kind} key")
    if kind == "http":
        method = case["request"]["method"]
        if match["method"] != method:
            raise CorpusError(
                f"{file_name}: ledger_key method {match['method']!r} differs from "
                f"request method {method!r}"
            )
    if kind == "mcp" and match["transport"] and match["server"] != "gobby":
        raise CorpusError(f"{file_name}: ledger_key {key!r} needs the gobby server")
    return dict(match.groupdict())


def _check_invocation(
    case: Mapping[str, Any], kind: str, key: Mapping[str, str | None], file_name: str
) -> None:
    invocation = case["invocation"]
    if not isinstance(invocation, dict):
        raise CorpusError(f"{file_name}: invocation must be an object")
    if kind == "mcp":
        if not (
            isinstance(invocation.get("server"), str)
            and isinstance(invocation.get("tool"), str)
            and isinstance(invocation.get("arguments"), dict)
        ):
            raise CorpusError(f"{file_name}: invocation needs a server, a tool and arguments")
        if invocation.get("transport") not in MCP_TRANSPORTS:
            raise CorpusError(
                f"{file_name}: invocation.transport must be one of {sorted(MCP_TRANSPORTS)}"
            )
        key_transport = key["transport"]
        if key_transport is not None and key_transport.lower() != invocation["transport"]:
            raise CorpusError(
                f"{file_name}: ledger_key {case['ledger_key']!r} names transport "
                f"{key_transport!r} but invocation.transport is {invocation['transport']!r}"
            )
        if (invocation["server"], invocation["tool"]) != (key["server"], key["tool"]):
            raise CorpusError(
                f"{file_name}: invocation {invocation['server']}:{invocation['tool']} "
                f"differs from ledger_key {case['ledger_key']!r}"
            )
    elif kind == "ws":
        frames = invocation.get("frames")
        if not isinstance(frames, list) or not all(isinstance(frame, str) for frame in frames):
            raise CorpusError(f"{file_name}: invocation.frames must be text frames")
        for frame in frames:
            frame_type = _frame_type(frame, file_name)
            if frame_type != key["type"]:
                raise CorpusError(
                    f"{file_name}: frame type {frame_type!r} differs from "
                    f"ledger_key {case['ledger_key']!r}"
                )
    else:
        argv = invocation.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise CorpusError(f"{file_name}: invocation.argv must be a non-empty string list")
        if argv[0] != key["verb"]:
            raise CorpusError(
                f"{file_name}: argv[0] {argv[0]!r} differs from ledger_key verb {key['verb']!r}"
            )
        stdin = invocation.get("stdin")
        if stdin is not None and not isinstance(stdin, str):
            raise CorpusError(f"{file_name}: invocation.stdin must be text or null")


def _frame_type(frame: str, file_name: str) -> Any:
    try:
        decoded = json.loads(frame)
    except json.JSONDecodeError as error:
        raise CorpusError(f"{file_name}: frame is not JSON: {frame!r}") from error
    if not isinstance(decoded, dict):
        raise CorpusError(f"{file_name}: frame is not a JSON object: {frame!r}")
    return decoded.get("type")


def _check_normalizers(normalizers: Any, file_name: str, *, allow_after: bool = False) -> None:
    """Validate a normalizer map: each value is a name, or ``{"name", "after"}`` for timestamp.

    ``after`` names a sibling column and is legal only where ``allow_after`` says so (db_delta).
    """
    if not isinstance(normalizers, dict):
        raise CorpusError(f"{file_name}: normalizers must be an object")
    for spec in normalizers.values():
        _, after = _normalizer_spec(spec, file_name)
        if after is not None and not allow_after:
            raise CorpusError(f"{file_name}: 'after' is declared only on db_delta normalizers")


def _normalizer_spec(spec: Any, file_name: str) -> tuple[str, str | None]:
    if isinstance(spec, str):
        name, after = spec, None
    elif isinstance(spec, dict) and set(spec) <= {"name", "after"} and "name" in spec:
        name, after = spec["name"], spec.get("after")
        if after is not None and (not isinstance(after, str) or not after):
            raise CorpusError(f"{file_name}: normalizer 'after' must name a column")
    else:
        raise CorpusError(f"{file_name}: normalizer {spec!r} is not a field normalizer")
    if not isinstance(name, str) or name not in FIELD_NORMALIZERS:
        raise CorpusError(f"{file_name}: normalizer {name!r} is not a field normalizer")
    if after is not None and name != "timestamp":
        raise CorpusError(f"{file_name}: only the timestamp normalizer takes 'after'")
    return name, after


def _check_pointer_keys(normalizers: Mapping[str, Any], file_name: str) -> None:
    """Case-level and side-effect normalizer keys are RFC 6901 pointers (see normalizers_v3)."""
    for pointer in normalizers:
        if not pointer.startswith("/"):
            raise CorpusError(f"{file_name}: normalizer key {pointer!r} must be a JSON pointer")


def _check_db_delta(entry: Any, file_name: str) -> None:
    if not isinstance(entry, dict):
        raise CorpusError(f"{file_name}: db_delta entries must be objects")
    table = entry.get("table")
    if not isinstance(table, str) or not table:
        raise CorpusError(f"{file_name}: db_delta table is required")
    key = entry.get("key")
    if not isinstance(key, list) or not key or not all(isinstance(col, str) for col in key):
        raise CorpusError(f"{file_name}: db_delta {table} key is required")
    columns = entry.get("columns")
    if columns != "all" and not (
        isinstance(columns, list) and columns and all(isinstance(col, str) for col in columns)
    ):
        raise CorpusError(f"{file_name}: db_delta {table} columns must be 'all' or a list")
    _check_normalizers(entry.get("normalizers", {}), file_name, allow_after=True)
    if "order" not in entry:
        raise CorpusError(f"{file_name}: db_delta {table} order is required")
    order = entry["order"]
    if not isinstance(order, str) or order not in ORDER_MODES:
        raise CorpusError(f"{file_name}: db_delta {table} order must be 'set' or 'as_recorded'")
    if order == "set" and "order_by" in entry:
        raise CorpusError(f"{file_name}: db_delta {table} order_by is not allowed with order 'set'")
    if order == "as_recorded":
        order_by = entry.get("order_by")
        if not isinstance(order_by, list) or not order_by:
            raise CorpusError(
                f"{file_name}: db_delta {table} order_by is required with order 'as_recorded'"
            )


def _check_side_effect(effect: Any, file_name: str) -> None:
    if (
        not isinstance(effect, dict)
        or not isinstance(effect.get("channel"), str)
        or effect["channel"] not in CHANNELS
        or not isinstance(effect.get("match"), dict)
    ):
        raise CorpusError(f"{file_name}: side effect needs a known channel and a match object")
    _check_normalizers(effect.get("normalizers", {}), file_name)
    _check_pointer_keys(effect.get("normalizers", {}), file_name)
