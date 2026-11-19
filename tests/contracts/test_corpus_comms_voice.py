"""Contracts for the communications and voice cases of the schema 3 corpus.

The corpus must hold a case for each of the 34 communications and voice ledger keys, 36 cases in
all, with a startup block that enables communications, a fixed Slack channel, voice masks, provider
edge side effects and webhook answers. A test that starts a daemon runs only after its inputs exist.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.contracts import corpus_v3, recorder_v3
from tests.contracts.comms.fake_edge import FakeEdge
from tests.contracts.recorder_daemon_v3 import (
    CORPUS_SLACK_CHANNEL_ID,
    RecorderDaemon,
    recorder_daemon,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
V3_DIR = corpus_v3.V3_DIR
STATUS_DIR = REPO_ROOT / "docs" / "contracts" / "parity-ledger-status.d"
STATUS_FILES = ("comms-cron-hub-nodes.json", "websocket-chat-voice-ui.json")
COMMS_VOICE_FAMILIES = frozenset({"communications", "voice"})
COMMS_VOICE_CASE_COUNT = 36
TOTAL_V3_CASE_COUNT = 41
COMMS_VOICE_LEDGER_KEY_COUNT = 34
GOBBY_CHAT_CHANNEL_ID = "00000000-0000-4000-8000-00000000c0c0"
CORPUS_SLACK_CHANNEL_NAME = "corpus-slack"
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECRET_MARKER = "corpus-fake-"
# The hosts each provider-reaching case contacts through the provider edge.
PROVIDER_REACHING_HOSTS: dict[str, tuple[str, ...]] = {
    "comms_channel_create": ("slack.com",),
    "comms_channel_update": ("slack.com",),
    "comms_send": ("slack.com",),
    "comms_attachment": ("slack.com", "upload.corpus.test"),
    "comms_mcp_send_message": ("slack.com",),
}
VOICE_CASES = ("voice_status", "voice_status_want_tts", "voice_transcribe_missing_file")
VOICE_REFUSAL_BODY = {"error": "grant lacks the required capability", "code": "wrong_capability"}
WEBHOOK_VERIFY_CASE = "comms_webhook_verify"
WEBHOOK_RECEIVE_CASE = "comms_webhook_receive"
README_HEADINGS = (
    "Communications enablement",
    "Seeded channel",
    "Voice masks",
    "Uncovered voice WebSocket rows",
)


def _v3_cases() -> list[dict[str, Any]]:
    return corpus_v3.load_cases(V3_DIR)


def _v3_case_named(name: str) -> dict[str, Any]:
    matches = [case for case in _v3_cases() if case.get("name") == name]
    assert len(matches) == 1, f"expected one v3 case named {name!r}, found {len(matches)}"
    return matches[0]


def _comms_voice_cases() -> list[dict[str, Any]]:
    return [case for case in _v3_cases() if case.get("family") in COMMS_VOICE_FAMILIES]


def _communications_startup() -> dict[str, Any] | None:
    startup = corpus_v3.load_manifest(V3_DIR).get("startup", {})
    communications: dict[str, Any] | None = startup.get("communications")
    return communications


def _startup_channels() -> list[dict[str, Any]]:
    startup = _communications_startup() or {}
    channels: list[dict[str, Any]] = list(startup.get("channels", []))
    return channels


def _is_comms_or_voice_key(key: str) -> bool:
    return (
        "/api/comms" in key
        or "/api/voice" in key
        or key.startswith(("MCP gobby-communications:", "MCP gobby-voice:"))
    )


def _comms_and_voice_ledger_keys() -> set[str]:
    keys: set[str] = set()
    for file_name in STATUS_FILES:
        rows = json.loads((STATUS_DIR / file_name).read_text(encoding="utf-8"))
        keys.update(key for key in rows if _is_comms_or_voice_key(key))
    return keys


def _wait_for_active(
    daemon: RecorderDaemon, channel_id: str, timeout_seconds: float
) -> dict[str, Any]:
    """Poll the channel's status until it is active without an init error, or the cap passes."""
    deadline = time.monotonic() + timeout_seconds
    status: dict[str, Any] = {}
    with httpx.Client(
        base_url=daemon.http_url, headers=daemon.operator_headers(), timeout=10.0
    ) as client:
        while True:
            response = client.get(f"/api/comms/channels/{channel_id}/status")
            if response.status_code == 200:
                status = response.json()
            else:
                status = {"http_status": response.status_code}
            if status.get("active") is True and status.get("init_error") is None:
                return status
            if time.monotonic() >= deadline:
                return status
            time.sleep(0.5)


class _ClearKeepingEdge(FakeEdge):
    """A provider edge that keeps the hits each clear discards.

    The recorder daemon clears the edge log once the startup channel is live, so its startup
    traffic falls outside every capture window. This edge keeps that traffic visible to a test.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cleared: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.cleared.extend(self.hits())
        super().clear()


@contextmanager
def _started_edge(root: Path) -> Iterator[_ClearKeepingEdge]:
    """Run a provider edge for the life of the block, so no daemon starts without one."""
    edge = _ClearKeepingEdge(root)
    edge.start()
    try:
        yield edge
    finally:
        edge.stop()


def _provider_edge_effects(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The provider_edge side effects a case declares, in declaration order."""
    return [
        effect
        for effect in case.get("side_effects", [])
        if effect.get("channel") == "provider_edge"
    ]


def _effect_hosts(effect: dict[str, Any]) -> list[str]:
    """The hosts a provider_edge side effect matches."""
    hosts: list[str] = effect.get("match", {}).get("hosts", [])
    return hosts


@pytest.mark.unit
def test_every_comms_and_voice_ledger_key_has_a_v3_case() -> None:
    for file_name in STATUS_FILES:
        status_file = STATUS_DIR / file_name
        assert status_file.is_file(), f"the parity ledger status file {status_file} does not exist"
    keys = _comms_and_voice_ledger_keys()
    assert len(keys) == COMMS_VOICE_LEDGER_KEY_COUNT, (
        f"expected {COMMS_VOICE_LEDGER_KEY_COUNT} comms and voice ledger keys, found {len(keys)}"
    )
    declared = {case["ledger_key"] for case in _v3_cases()}
    missing = sorted(keys - declared)
    assert missing == [], f"no v3 case carries these ledger keys: {missing}"


@pytest.mark.unit
def test_v3_holds_thirty_six_comms_and_voice_cases() -> None:
    cases = _comms_voice_cases()
    assert len(cases) == COMMS_VOICE_CASE_COUNT, (
        f"expected {COMMS_VOICE_CASE_COUNT} communications and voice cases, found {len(cases)}"
    )


@pytest.mark.e2e
def test_comms_routes_answer_once_communications_is_enabled_at_start(tmp_path: Path) -> None:
    startup = _communications_startup()
    assert startup is not None, "the v3 manifest declares no communications startup block"
    with _started_edge(tmp_path) as edge:
        assert edge.address[1] != 0, "the provider edge bound no port"
        with (
            recorder_v3.open_run_database() as run,
            recorder_daemon(
                base_url=run.base_url, database_url=run.url, startup=startup, edge=edge
            ) as daemon,
        ):
            response = httpx.get(
                f"{daemon.http_url}/api/comms/channels",
                headers=daemon.operator_headers(),
                timeout=30.0,
            )
    assert response.status_code == 200, f"GET /api/comms/channels answered {response.status_code}"


@pytest.mark.e2e
def test_corpus_slack_channel_is_live_at_the_first_request(tmp_path: Path) -> None:
    assert UUID_TEXT.fullmatch(CORPUS_SLACK_CHANNEL_ID), (
        f"the fixed channel id is not a UUID: {CORPUS_SLACK_CHANNEL_ID!r}"
    )
    startup = _communications_startup()
    assert startup is not None, "the v3 manifest declares no communications startup block"
    with _started_edge(tmp_path) as edge:
        assert edge.address[1] != 0, "the provider edge bound no port"
        with (
            recorder_v3.open_run_database() as run,
            recorder_daemon(
                base_url=run.base_url, database_url=run.url, startup=startup, edge=edge
            ) as daemon,
        ):
            status = _wait_for_active(daemon, CORPUS_SLACK_CHANNEL_ID, timeout_seconds=60.0)
        assert status.get("active") is True, f"the startup Slack channel is not active: {status}"
        assert status.get("init_error") is None, (
            f"the startup Slack channel failed to initialize: {status}"
        )
        arrivals = [hit["host"] for hit in [*edge.cleared, *edge.hits()]]
    assert "slack.com" in arrivals, (
        f"the startup Slack channel reached no slack.com through the provider edge: {arrivals}"
    )


@pytest.mark.unit
def test_fixed_channel_id_is_a_uuid_and_not_the_gobby_chat_id() -> None:
    assert UUID_TEXT.fullmatch(CORPUS_SLACK_CHANNEL_ID), (
        f"the fixed channel id is not a UUID: {CORPUS_SLACK_CHANNEL_ID!r}"
    )
    assert CORPUS_SLACK_CHANNEL_ID != GOBBY_CHAT_CHANNEL_ID, (
        "the fixed channel id is the dropped gobby_chat id"
    )
    startup_ids = [row.get("id") for row in _startup_channels()]
    assert startup_ids == [CORPUS_SLACK_CHANNEL_ID], (
        f"the communications startup block declares channel ids {startup_ids}, "
        "expected only the fixed channel id"
    )


@pytest.mark.unit
def test_no_comms_or_voice_case_uses_the_dropped_gobby_chat_id() -> None:
    cases = _comms_voice_cases()
    assert len(cases) >= COMMS_VOICE_CASE_COUNT, (
        f"scanned {len(cases)} communications and voice cases, expected {COMMS_VOICE_CASE_COUNT}"
    )
    carrying = [
        case["name"] for case in cases if GOBBY_CHAT_CHANNEL_ID in json.dumps(case, sort_keys=True)
    ]
    assert carrying == [], f"cases carry the dropped gobby_chat id: {carrying}"


@pytest.mark.unit
def test_voice_cases_record_the_grant_refusal_with_no_masks() -> None:
    """Voice is disabled, so a valid grant lacks the audio capability and the route refuses it.

    The engine and availability fields only appear in a 200 answer, so the recorded refusal
    carries none of them and no mask resolves. Each voice case presents a runtime grant and
    records the refusal body in the HTTP response, under the response body prefix.
    """
    for name in VOICE_CASES:
        case = _v3_case_named(name)
        assert case["credential"] == "grant", f"{name} does not present a runtime grant"
        assert case["response"]["status"] == 401, f"{name} did not refuse the grant"
        assert case["response"]["body"] == VOICE_REFUSAL_BODY, f"{name} answered a different body"
        assert case["mask"] == [], f"{name} masks a key that its refusal does not carry"


@pytest.mark.unit
def test_every_provider_reaching_case_declares_a_provider_edge_side_effect() -> None:
    cases = _comms_voice_cases()
    assert len(cases) == COMMS_VOICE_CASE_COUNT, (
        f"expected {COMMS_VOICE_CASE_COUNT} communications and voice cases, found {len(cases)}"
    )
    by_name = {case["name"]: case for case in cases}
    missing = sorted(set(PROVIDER_REACHING_HOSTS) - set(by_name))
    assert missing == [], f"these are not communications or voice cases: {missing}"
    for case in cases:
        name = case["name"]
        effects = _provider_edge_effects(case)
        declared = sorted(host for effect in effects for host in _effect_hosts(effect))
        expected = sorted(PROVIDER_REACHING_HOSTS.get(name, ()))
        assert declared == expected, (
            f"{name} declares provider_edge hosts {declared}, expected {expected}"
        )
        for effect in effects:
            hosts = set(_effect_hosts(effect))
            recorded = {hit.get("host") for hit in effect.get("recorded", [])}
            assert recorded == hosts, (
                f"{name} records provider_edge hosts {sorted(recorded, key=str)}, "
                f"declared {sorted(hosts)}"
            )


@pytest.mark.unit
def test_no_committed_case_holds_a_generated_secret_value() -> None:
    manifest = corpus_v3.load_manifest(V3_DIR)
    assert len(manifest["cases"]) == TOTAL_V3_CASE_COUNT, (
        f"expected {TOTAL_V3_CASE_COUNT} schema 3 cases, found {len(manifest['cases'])}"
    )
    leaking = [
        name
        for name in manifest["cases"]
        if SECRET_MARKER in (V3_DIR / name).read_text(encoding="utf-8")
    ]
    assert leaking == [], f"committed cases hold a generated secret value: {leaking}"


@pytest.mark.unit
def test_v3_readme_documents_comms_voice_scope() -> None:
    readme = V3_DIR / "README.md"
    assert readme.is_file(), "tests/contracts/v3/README.md does not exist"
    headings = {
        line.lstrip("#").strip()
        for line in readme.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    }
    for heading in README_HEADINGS:
        assert heading in headings, f"the v3 README has no heading {heading!r}"


@pytest.mark.unit
def test_startup_slack_channel_declares_a_default_destination() -> None:
    rows = [row for row in _startup_channels() if row.get("name") == CORPUS_SLACK_CHANNEL_NAME]
    assert len(rows) == 1, (
        f"the communications startup block declares {len(rows)} {CORPUS_SLACK_CHANNEL_NAME!r} "
        "channels, expected 1"
    )
    destination = rows[0].get("config", {}).get("default_destination")
    assert isinstance(destination, str) and destination, (
        "the startup Slack channel declares no default_destination"
    )


@pytest.mark.unit
def test_webhook_cases_answer_as_the_routes_document() -> None:
    verify = _v3_case_named(WEBHOOK_VERIFY_CASE)["response"]
    assert verify["status"] == 200, f"the webhook GET answered {verify['status']}"
    assert str(verify["headers"].get("content-type", "")).startswith("text/plain")
    assert verify["body"] == "corpus-challenge", f"the webhook GET answered {verify['body']!r}"
    receive = _v3_case_named(WEBHOOK_RECEIVE_CASE)["response"]
    assert receive["status"] == 400, f"the webhook POST answered {receive['status']}"
    assert receive["body"] == {
        "detail": f"Webhook signature verification failed for channel '{CORPUS_SLACK_CHANNEL_NAME}'"
    }, f"the webhook POST answered {receive['body']!r}"
