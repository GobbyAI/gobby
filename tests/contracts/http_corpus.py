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
from pathlib import Path
from typing import Any

import httpx

CORPUS_DIR = Path(__file__).resolve().parent / "http"
MANIFEST_NAME = "manifest.json"
MASK = "@mask@"
SECRET = "@secret@"
PARITY_VALUES = frozenset({"proxy", "native"})
ORIGIN_VALUES = frozenset({"python", "gdaemon"})
BACKEND_VALUES = frozenset({"up", "down"})
CREDENTIALS = frozenset({"none", "operator", "grant"})
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
    Rejects any case whose ``schema_version`` differs from the manifest's, whose family
    the manifest lacks, or whose ``backend`` is missing or neither ``up`` nor ``down``.
    """
    manifest = load_manifest(corpus_dir)
    version = manifest["schema_version"]
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
        if case.get("backend") not in BACKEND_VALUES:
            raise CorpusError(
                f"{file_name}: backend {case.get('backend')!r} is neither 'up' nor 'down'"
            )
        if family["origin"] == "python" and case["backend"] == "up":
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


def recorded_case(case: Mapping[str, Any], response: httpx.Response) -> dict[str, Any]:
    """Return ``case`` with its ``response`` replaced by the normalized capture."""
    recorded = copy.deepcopy(dict(case))
    recorded["response"] = normalize_response(case, response)
    return {key: recorded[key] for key in _CASE_KEY_ORDER}


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


def replay_case(
    client: httpx.Client,
    case: Mapping[str, Any],
    *,
    operator: Mapping[str, str],
    grant: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(expected, actual)`` normalized responses for a committed case."""
    response = send_case(client, case, operator=operator, grant=grant)
    expected: dict[str, Any] = normalize_case(case)["response"]
    return expected, normalize_response(case, response)
