"""Leaf helpers for the v3 recorder: the error, the seed sender, the REST tool call, the rules for
recorded db values and the provider-edge capture.

This module imports nothing from ``recorder_v3``, so the recorder and its tests can both load it
without a cycle.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from gobby.communications.models import ChannelConfig
from gobby.runtime_grants.handshake import encode_grant_header
from gobby.runtime_grants.schema import GrantBundle
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.secrets import SecretStore
from tests.contracts import http_corpus
from tests.contracts.comms.fake_edge import DECODE_ERROR_KEY

# The quiet period and the cap that bound every capture window. The recorder's other channels
# drain with the same values.
CAPTURE_QUIET_SECONDS = 0.5
CAPTURE_CAP_SECONDS = 5.0
REST_TOOL_PREFIX = "/api/mcp/"
# The machine and project the recording daemons are built for. The handshake issues a grant for
# this pair, so the daemon's own machine is the one the grant names.
E2E_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
HANDSHAKE_PATH = "/api/runtime/handshake"
GRANT_HEADER = "X-Gobby-Runtime-Grant"


class RecorderError(RuntimeError):
    """A recording or replay could not run to a comparable result."""


class ProviderEdgeLog(Protocol):
    """A provider edge's arrival log: every arrival since the last clear, in arrival order."""

    def hits(self) -> list[dict[str, Any]]: ...

    def clear(self) -> None: ...


def apply_startup(
    entry: Mapping[str, Any],
    *,
    database: PostgresHubDatabase,
    home: Path,
    machine_id: str,
) -> None:
    """Apply one family's startup block before its daemon starts: config, channels, then secrets.

    The config patch is one compare-and-swap at the current revision. Each channel row is written
    with its fixed id; the store overwrites its timestamps. Each secret gets a value generated at
    run time, written under the daemon's home so the daemon decrypts it with the same key file.
    No generated value is returned or recorded.
    """
    config: dict[str, Any] = dict(entry.get("config", {}))
    if config:
        mutations = ConfigMutations(database)
        mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(values=config),
            source="corpus-startup",
        )
    store = LocalCommunicationsStore(database, project_id="", machine_id=machine_id)
    now = datetime.now(UTC)
    for row in entry.get("channels", []):
        store.create_channel(
            ChannelConfig(
                id=str(row["id"]),
                channel_type=str(row["channel_type"]),
                name=str(row["name"]),
                enabled=bool(row["enabled"]),
                config_json=dict(row["config"]),
                created_at=now,
                updated_at=now,
                webhook_secret=None,
            )
        )
    secrets = SecretStore(database, gobby_home=home)
    for name in entry.get("secrets", []):
        secrets.set(str(name), f"corpus-fake-{uuid.uuid4()}")


def mint_runtime_grant(client: httpx.Client, *, operator: Mapping[str, str]) -> str:
    """Ask the daemon's handshake for a runtime grant and return its presentation header value.

    The operator bearer authenticates the request. The daemon issues the grant for the recording
    machine and project, and the header is the grant's canonical encoding.
    """
    response = client.post(
        HANDSHAKE_PATH,
        json={"machine_id": E2E_MACHINE_ID, "project_id": E2E_PROJECT_ID},
        headers=dict(operator),
    )
    if response.status_code != 200:
        raise RecorderError(f"the runtime handshake answered HTTP {response.status_code}")
    grant = GrantBundle.model_validate(response.json()["grant"])
    return encode_grant_header(grant)


def recorder_credential_headers(
    client: httpx.Client, credential: str, operator: Mapping[str, str]
) -> dict[str, str]:
    """The headers a recorded request sends for its credential.

    The grant credential sends the operator bearer with a runtime grant minted by the handshake
    on the same client, so the grant is the daemon's own and is minted once per request.
    """
    if credential == "grant":
        header = mint_runtime_grant(client, operator=operator)
        return {**dict(operator), GRANT_HEADER: header}
    return http_corpus.credential_headers(credential, operator=operator, grant={})


def send_seed_http(
    client: httpx.Client, step: Mapping[str, Any], *, operator: Mapping[str, str]
) -> None:
    """Send one seed step and refuse a failed answer.

    Every seed answers HTTP 200. A seed on the REST tool route must also answer a JSON body whose
    ``success`` is true, because that route answers a tool-level failure with HTTP 200 as well.
    """
    headers = recorder_credential_headers(client, step["credential"], operator)
    response = client.request(step["method"], step["path"], headers=headers, json=step.get("body"))
    path = step["path"]
    if response.status_code != 200:
        raise RecorderError(f"seed {path} answered {response.status_code}")
    if not path.startswith(REST_TOOL_PREFIX):
        return
    try:
        body = response.json()
    except ValueError as error:
        raise RecorderError(f"seed {path} answered no JSON body") from error
    if not (isinstance(body, dict) and body.get("success") is True):
        raise RecorderError(f"seed {path} did not report success: {body!r}")


def send_mcp_rest(
    client: httpx.Client, invocation: Mapping[str, Any], *, operator: Mapping[str, str]
) -> Any:
    """Call one proxied tool through its REST route and return the JSON body the route answers.

    The body is the recording. Only a non-200 answer is refused here; what a tool-level result
    means is left to the case.
    """
    server, tool = invocation["server"], invocation["tool"]
    response = client.post(
        f"{REST_TOOL_PREFIX}{server}/tools/{tool}",
        headers=dict(operator),
        json=invocation["arguments"],
    )
    if response.status_code != 200:
        raise RecorderError(f"REST tool {server}:{tool} answered {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise RecorderError(f"REST tool {server}:{tool} answered no JSON body") from error


def _carries_secret(value: Any) -> bool:
    """True when a secret-keyed field sits anywhere in the value; redaction would change it."""
    return bool(http_corpus.redact_secrets(value) != value)


def recorded_db_delta(value: Mapping[str, Any], case_name: str) -> dict[str, Any]:
    """Redact one db_delta value for writing: secret-keyed fields never reach a recording.

    Added and removed rows are redacted in full, the same rule the comparison applies to both
    sides, so the stored value and the comparison agree. A changed value in a secret-keyed column,
    or one that carries a secret-keyed field, is refused: writing it would commit the secret, and
    masking it would hide its change from the comparison. A decision is needed for those.
    """
    changed: list[dict[str, Any]] = []
    for item in value["changed"]:
        column = item["column"]
        if (
            column in http_corpus.SECRET_KEYS
            or _carries_secret(item["before"])
            or _carries_secret(item["after"])
        ):
            raise RecorderError(
                f"{case_name}: a changed value in secret-keyed column {column!r} cannot be "
                "recorded; the case needs a decision"
            )
        changed.append({**item, "key": http_corpus.redact_secrets(item["key"])})
    return {
        "added": http_corpus.redact_secrets(value["added"]),
        "removed": http_corpus.redact_secrets(value["removed"]),
        "changed": changed,
    }


class ProviderEdgeCapture:
    """The arrivals at one provider edge for the listed hosts, in arrival order.

    Each capture item is the request signature (host, method and path) and the arrival's
    sanitized ``semantic`` fields: destination, text, attachment filename and the SHA-256 of the
    uploaded bytes. An arrival logged with no ``semantic`` (an edge that keeps signatures only)
    is captured as its signature, which then differs from a recording that carries semantics.
    An arrival the edge could not decode keeps its ``DECODE_ERROR_KEY`` marker, so it differs
    from a well-formed request that sends no semantic field. Entering the capture clears the
    edge log, so the window holds only the arrivals its request makes. Headers and raw bodies
    are never kept.
    """

    def __init__(self, edge: ProviderEdgeLog, hosts: Sequence[str]) -> None:
        self._edge = edge
        self._hosts = frozenset(hosts)

    def __enter__(self) -> ProviderEdgeCapture:
        self._edge.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        """The log is left as the request left it, for ``drain`` to read."""

    def hits(self) -> list[dict[str, Any]]:
        return [
            {
                "host": arrival["host"],
                "method": arrival["method"],
                "path": arrival["path"],
                **({"semantic": dict(arrival["semantic"])} if "semantic" in arrival else {}),
                **(
                    {DECODE_ERROR_KEY: arrival[DECODE_ERROR_KEY]}
                    if DECODE_ERROR_KEY in arrival
                    else {}
                ),
            }
            for arrival in self._edge.hits()
            if arrival["host"] in self._hosts
        ]

    def drain(self) -> list[dict[str, Any]]:
        """Return the listed hits once the log is quiet for the quiet period, or at the cap."""
        started = time.monotonic()
        seen, last_change = len(self._edge.hits()), started
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            count = len(self._edge.hits())
            if count != seen:
                seen, last_change = count, now
            if now - last_change >= CAPTURE_QUIET_SECONDS or now - started >= CAPTURE_CAP_SECONDS:
                return self.hits()
