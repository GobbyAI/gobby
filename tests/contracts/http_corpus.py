"""Loader, normalization, recorder, and replay helpers for the HTTP contract corpus.

Each case under ``tests/contracts/http/`` is an executable recipe: ``request`` holds
the exact method, path, query, headers, and body Python sends, and ``credential``
names the header recipe added immediately before sending. The recorded
``response`` is normalized (secret redaction, then RFC 6901 masks) so recordings
are deterministic and replay compares like with like. The Rust harness applies
the same rules.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from scripts.generate_parity_ledger import ADDITIVE_SURFACES, collect_surfaces
from scripts.parity_waivers import (
    DIFF,
    NATIVE_EXTENSION,
    PASSED,
    WAIVED,
    WAIVERS_PATH,
    Waiver,
    cite_waiver,
    load_waivers,
)

CORPUS_DIR = Path(__file__).resolve().parent / "http"
MANIFEST_NAME = "manifest.json"
MASK = "@mask@"
SECRET = "@secret@"
PARITY_VALUES = frozenset({"proxy", "native"})
ORIGIN_VALUES = frozenset({"python", "gdaemon"})
BACKEND_VALUES = frozenset({"up", "down"})
CREDENTIALS = frozenset({"none", "operator", "grant"})
SCHEMA_VERSIONS = frozenset({2, 3})
RESPONSE_HEADER_ALLOWLIST = (
    "cache-control",
    "content-type",
    "retry-after",
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
)
SECRET_KEYS = frozenset(
    {
        "dsn",
        "password",
        "api_key",
        "token",
        "deployment_token",
        "payload_checksum",
        "signature",
        "proof",
    }
)
_CASE_KEY_ORDER = (
    "schema_version",
    "name",
    "family",
    "backend",
    "credential",
    "request",
    "response",
    "waiver",
    "native_response",
    "mask",
)


class CorpusError(ValueError):
    """A corpus file violates the fixture contract."""


def load_manifest(corpus_dir: Path = CORPUS_DIR) -> dict[str, Any]:
    manifest = json.loads((corpus_dir / MANIFEST_NAME).read_text())
    if not isinstance(manifest, dict):
        raise CorpusError("manifest must be a JSON object")
    return manifest


def load_cases(corpus_dir: Path = CORPUS_DIR) -> list[dict[str, Any]]:
    """Load the cases Python records and replays, in manifest order.

    Those are the ``backend: up`` cases of ``origin: python`` families, because the
    e2e backend is always up; ``down`` cases are hand-authored and replayed by gdaemon.
    Rejects a manifest whose ``schema_version`` is neither 2 nor 3, any case whose
    ``schema_version`` differs from the manifest's, whose family the manifest lacks, or
    whose ``backend`` is missing or neither ``up`` nor ``down``.
    """
    manifest = load_manifest(corpus_dir)
    version = manifest["schema_version"]
    if not isinstance(version, int) or version not in SCHEMA_VERSIONS:
        raise CorpusError(f"manifest schema_version {version!r} is neither 2 nor 3")
    families: Mapping[str, Mapping[str, str]] = manifest["families"]
    cases: list[dict[str, Any]] = []
    for file_name in manifest["cases"]:
        case = json.loads((corpus_dir / file_name).read_text())
        if case.get("schema_version") != version:
            raise CorpusError(
                f"{file_name}: schema_version {case.get('schema_version')!r} "
                f"differs from manifest {version!r}"
            )
        family = families.get(case["family"])
        if family is None:
            raise CorpusError(f"{file_name}: family {case['family']!r} is not in the manifest")
        backend = case.get("backend")
        if not isinstance(backend, str) or backend not in BACKEND_VALUES:
            raise CorpusError(f"{file_name}: backend {backend!r} is neither 'up' nor 'down'")
        if family["origin"] == "python" and case["backend"] == "up":
            waiver_id = case.get("waiver")
            if isinstance(waiver_id, str) and waiver_id.endswith(".native-extension"):
                raise CorpusError(
                    f"{file_name}: native-extension cases require a gdaemon-origin family"
                )
            cases.append(case)
    return cases


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise CorpusError(f"mask pointer {pointer!r} must start with '/'")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _index(items: list[Any], token: str) -> int | None:
    if token.isdigit() and int(token) < len(items):
        return int(token)
    return None


def _child(node: Any, token: str) -> Any:
    if isinstance(node, dict):
        return node.get(token)
    if isinstance(node, list):
        index = _index(node, token)
        return None if index is None else node[index]
    return None


def apply_masks(document: Any, pointers: Iterable[str]) -> Any:
    """Return a copy of ``document`` with each pointed-at value replaced by ``MASK``.

    A pointer to an absent field leaves the document unchanged; it never creates one.
    """
    masked = copy.deepcopy(document)
    for pointer in pointers:
        tokens = _pointer_tokens(pointer)
        parent: Any = masked
        for token in tokens[:-1]:
            parent = _child(parent, token)
        last = tokens[-1]
        if isinstance(parent, dict) and last in parent:
            parent[last] = MASK
        elif isinstance(parent, list):
            index = _index(parent, last)
            if index is not None:
                parent[index] = MASK
    return masked


def redact_secrets(value: Any) -> Any:
    """Replace the value of every ``SECRET_KEYS`` object key, at any depth, with ``SECRET``."""
    if isinstance(value, dict):
        return {
            key: SECRET if key in SECRET_KEYS else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def credential_headers(
    credential: str,
    *,
    operator: Mapping[str, str],
    grant: Mapping[str, str],
) -> dict[str, str]:
    """Materialize a case's ``credential`` recipe into request headers."""
    if credential == "none":
        return {}
    if credential == "operator":
        return dict(operator)
    if credential == "grant":
        return dict(grant)
    raise CorpusError(f"unknown credential recipe {credential!r}")


def send_case(
    client: httpx.Client,
    case: Mapping[str, Any],
    *,
    operator: Mapping[str, str],
    grant: Mapping[str, str],
) -> httpx.Response:
    """Send a case's request exactly as persisted, plus its credential headers."""
    request = case["request"]
    headers = credential_headers(case["credential"], operator=operator, grant=grant)
    headers.update(request["headers"])
    return client.request(
        request["method"],
        request["path"],
        params=request["query"],
        headers=headers,
        json=request["body"],
    )


def _response_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.text


def _normalized(document: dict[str, Any], mask: Iterable[str]) -> dict[str, Any]:
    response = document["response"]
    response["body"] = redact_secrets(response["body"])
    normalized: dict[str, Any] = apply_masks(document, mask)
    return normalized


def normalize_response(case: Mapping[str, Any], response: httpx.Response) -> dict[str, Any]:
    """Build the case's normalized ``response``: allowlisted headers, redacted and masked body."""
    headers = {
        name: response.headers[name]
        for name in RESPONSE_HEADER_ALLOWLIST
        if name in response.headers
    }
    captured = {
        "response": {
            "status": response.status_code,
            "headers": headers,
            "body": _response_body(response),
        }
    }
    normalized: dict[str, Any] = _normalized(captured, case["mask"])["response"]
    return normalized


def normalize_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Apply redaction and masks to a committed case, so replay compares normalized forms."""
    return _normalized(copy.deepcopy(dict(case)), case["mask"])


# Build-derived identity: the gdaemon that answers decides these values, so replay masks
# them and the test suite pins them to the source tree instead.
REPLAY_BUILD_MASKS: dict[str, tuple[str, ...]] = {
    "runtime_handshake": ("/response/body/grant/schema_identity",),
}


def replay_view(case: Mapping[str, Any]) -> dict[str, Any]:
    """Return the committed case with build-derived fields masked for replay comparison only."""
    extra = REPLAY_BUILD_MASKS.get(str(case.get("family")), ())
    return {**dict(case), "mask": [*case["mask"], *extra]}


def recorded_case(case: Mapping[str, Any], response: httpx.Response) -> dict[str, Any]:
    """Capture Python responses while retaining waiver and native contract provenance."""
    recorded = copy.deepcopy(dict(case))
    waiver_id = recorded.get("waiver")
    if isinstance(waiver_id, str) and waiver_id.endswith(".native-extension"):
        recorded.pop("response", None)
    else:
        recorded["response"] = normalize_response(case, response)
    return {key: recorded[key] for key in _CASE_KEY_ORDER if key in recorded}


def case_bytes(case: Mapping[str, Any]) -> bytes:
    return (json.dumps(case, indent=2, ensure_ascii=False) + "\n").encode()


def record_cases(
    client: httpx.Client,
    cases: Iterable[Mapping[str, Any]],
    out_dir: Path,
    *,
    operator: Mapping[str, str],
    grant: Mapping[str, str],
) -> list[Path]:
    """Send each case and write its normalized recording to ``out_dir/<name>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for case in cases:
        response = send_case(client, case, operator=operator, grant=grant)
        path = out_dir / f"{case['name']}.json"
        path.write_bytes(case_bytes(recorded_case(case, response)))
        written.append(path)
    return written


def load_replay_waivers(path: Path = WAIVERS_PATH) -> dict[str, Waiver]:
    """Load the committed waiver manifest against the ledger's surface inventory."""
    return load_waivers(path, surfaces=collect_surfaces(), additive_surfaces=ADDITIVE_SURFACES)


def replay_case(
    client: httpx.Client,
    case: Mapping[str, Any],
    *,
    operator: Mapping[str, str],
    grant: Mapping[str, str],
    waivers: Mapping[str, Waiver],
) -> tuple[ReplayResult, dict[str, Any]]:
    """Send a committed case and classify its normalized response; return both."""
    response = send_case(client, case, operator=operator, grant=grant)
    view = replay_view(case)
    actual = normalize_response(view, response)
    return classify_replay(view, actual, waivers), actual


@dataclass(frozen=True)
class ReplayResult:
    """How one replayed case classifies, with the waiver citation when one applies."""

    outcome: str
    waiver: str | None = None
    decision: str | None = None
    pointer: str | None = None


# Kinds whose responses are HTTP-shaped (status, headers, body). The ws and cli kinds answer
# with a frame list and an exit/stdout/stderr object, so their responses are redacted whole.
_HTTP_SHAPED_KINDS = frozenset({"http"})


def _case_kind(case: Mapping[str, Any]) -> str:
    """Return a case's kind; schema 2 cases carry none and are ``http``."""
    kind: str = case.get("kind", "http")
    return kind


def _redacted_response(kind: str, response: Any) -> Any:
    """Redact one response by its kind, keeping the v2 body rule for HTTP-shaped answers."""
    if kind in _HTTP_SHAPED_KINDS:
        return {**response, "body": redact_secrets(response["body"])}
    return redact_secrets(response)


def _entry_values(case: Mapping[str, Any], field: str) -> list[Any]:
    """Return the recorded value of each declared ``db_delta`` or ``side_effects`` entry."""
    values: list[Any] = []
    for index, entry in enumerate(case.get(field, [])):
        if "recorded" not in entry:
            raise CorpusError(f"{case['name']}: {field}[{index}] has no recorded value")
        values.append(entry["recorded"])
    return values


def _observed_values(
    case: Mapping[str, Any], observed: Mapping[str, Any] | None
) -> tuple[list[Any], list[Any]]:
    """Return the replay's db_delta values and side-effect captures, one per declared entry.

    Only schema 3 cases declare db_delta and side_effects, so only they take an observation.
    """
    if case.get("schema_version") != 3:
        if observed is not None:
            raise CorpusError(f"{case['name']}: only schema 3 cases take observed values")
        return [], []
    if observed is None:
        raise CorpusError(f"{case['name']}: schema 3 classification needs observed values")
    for field in ("db_delta", "side_effects"):
        if field not in observed:
            raise CorpusError(f"{case['name']}: observed {field} is missing")
    return list(observed["db_delta"]), list(observed["side_effects"])


def _comparison(
    case: Mapping[str, Any], response: Any, db_values: list[Any], effect_values: list[Any]
) -> dict[str, Any]:
    """Normalize one comparison set in case coordinates: response, db_delta and side effects.

    Each db_delta or side-effect value sits under ``recorded`` at its declaration index, so an
    RFC 6901 mask pointer addresses the recording and the observation alike.
    """
    document = {
        "response": _redacted_response(_case_kind(case), response),
        "db_delta": [{"recorded": redact_secrets(value)} for value in db_values],
        "side_effects": [{"recorded": redact_secrets(value)} for value in effect_values],
    }
    normalized: dict[str, Any] = apply_masks(document, case["mask"])
    return normalized


def _escape_pointer_token(token: Any) -> str:
    """Escape one object key as an RFC 6901 reference token."""
    return str(token).replace("~", "~0").replace("/", "~1")


def _first_difference(expected: Any, actual: Any, pointer: str = "") -> str | None:
    """Return the first RFC 6901 pointer where two normalized documents differ, or None."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in dict.fromkeys([*expected, *actual]):
            child = f"{pointer}/{_escape_pointer_token(key)}"
            if key not in expected or key not in actual:
                return child
            difference = _first_difference(expected[key], actual[key], child)
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return pointer
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_difference(left, right, f"{pointer}/{index}")
            if difference is not None:
                return difference
        return None
    if type(expected) is type(actual) and expected == actual:
        return None
    return pointer


def _citation(case: Mapping[str, Any]) -> dict[str, str]:
    """Return the surface a cited case names: its method and path for http, else its ledger key."""
    if _case_kind(case) == "http":
        request = case["request"]
        return {"method": request["method"], "path": request["path"]}
    return {"key": case["ledger_key"]}


def _difference(
    case: Mapping[str, Any],
    expected_response: Any,
    actual: Any,
    observation: tuple[list[Any], list[Any]],
) -> str | None:
    """Return the first pointer where the replay departs from the expected response and recording."""
    recorded_db = _entry_values(case, "db_delta")
    recorded_effects = _entry_values(case, "side_effects")
    expected = _comparison(case, expected_response, recorded_db, recorded_effects)
    observed_db, observed_effects = observation
    return _first_difference(expected, _comparison(case, actual, observed_db, observed_effects))


def classify_replay(
    case: Mapping[str, Any],
    actual: Any,
    waivers: Mapping[str, Waiver],
    *,
    observed: Mapping[str, Any] | None = None,
) -> ReplayResult:
    """Classify one replayed case of any kind against its recording, or its native answer.

    ``actual`` is the replayed response. A schema 3 case also takes ``observed``, the replayed
    ``db_delta`` values and ``side_effects`` captures in declaration order; a schema 2 case takes
    none. An unwaived case passes only when its whole comparison set equals the recording. A
    cited case matches its ``native_response`` on the response and the recording on the rest,
    and a native-extension waiver never reports Python success. A diff names the first pointer
    where the replay departs from the expected set.
    """
    observation = _observed_values(case, observed)
    waiver_id = case.get("waiver")
    if waiver_id is None:
        if "response" not in case:
            raise CorpusError(f"{case['name']} has no Python response and no waiver")
        difference = _difference(case, case["response"], actual, observation)
        if difference is None:
            return ReplayResult(outcome=PASSED)
        return ReplayResult(outcome=DIFF, pointer=difference)
    waiver = cite_waiver(waivers, waiver_id, kind=_case_kind(case), **_citation(case))
    if waiver.native_extension and "response" in case:
        raise CorpusError(f"Native-extension waiver {waiver_id!r} cannot cite a Python response")
    native = case.get("native_response")
    if native is None:
        raise CorpusError(f"Waived case {waiver_id!r} must assert its native_response")
    difference = _difference(case, native, actual, observation)
    if difference is not None:
        return ReplayResult(outcome=DIFF, pointer=difference)
    outcome = NATIVE_EXTENSION if waiver.native_extension else WAIVED
    return ReplayResult(outcome=outcome, waiver=waiver_id, decision=waiver.decision)
