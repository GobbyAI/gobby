"""Contracts for the communications and voice cases of the schema 3 corpus.

The corpus must hold a case for each of the 34 communications and voice ledger keys, 36 cases in
all, with a startup block that enables communications, a fixed Slack channel, voice masks, provider
edge side effects and webhook answers. A test that starts a daemon runs only after its inputs exist.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import socket
import ssl
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.parity_waivers import DIFF, PASSED
from tests.contracts import corpus_v3, recorder_v3
from tests.contracts.comms.fake_edge import DECODE_ERROR_KEY, FakeEdge
from tests.contracts.http_corpus import classify_replay
from tests.contracts.normalizers_v3 import Window, normalize_captures
from tests.contracts.recorder_daemon_v3 import (
    CORPUS_SLACK_CHANNEL_ID,
    RecorderDaemon,
    recorder_daemon,
)
from tests.contracts.recorder_v3_support import ProviderEdgeCapture

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
SLACK_DESTINATION = "C0CORPUS0001"
ATTACHMENT_SHA256 = hashlib.sha256(b"corpus attachment line one\n").hexdigest()
EMPTY_SEMANTIC = {"destination": "", "text": "", "filename": "", "payload_sha256": ""}


def _hit(host: str, method: str, path: str, **semantic: str) -> dict[str, Any]:
    """One provider edge capture item: the request signature and its sanitized semantics."""
    return {
        "host": host,
        "method": method,
        "path": path,
        "semantic": {**EMPTY_SEMANTIC, **semantic},
    }


_AUTH_TEST = _hit("slack.com", "POST", "/api/auth.test")
# The exact ordered provider requests each provider-reaching case makes, with what each one sends.
PROVIDER_SEQUENCES: dict[str, tuple[dict[str, Any], ...]] = {
    "comms_channel_create": (_AUTH_TEST,),
    "comms_channel_update": (_AUTH_TEST,),
    "comms_send": (
        _hit(
            "slack.com",
            "POST",
            "/api/chat.postMessage",
            destination=SLACK_DESTINATION,
            text="corpus message one",
        ),
    ),
    "comms_attachment": (
        _hit("slack.com", "POST", "/api/files.getUploadURLExternal", filename="corpus-errors.txt"),
        _hit("upload.corpus.test", "PUT", "/upload/corpus-1", payload_sha256=ATTACHMENT_SHA256),
        _hit(
            "slack.com",
            "POST",
            "/api/files.completeUploadExternal",
            destination=SLACK_DESTINATION,
            text="corpus caption",
            filename="corpus-errors.txt",
        ),
    ),
    "comms_mcp_send_message": (
        _hit(
            "slack.com",
            "POST",
            "/api/chat.postMessage",
            destination=SLACK_DESTINATION,
            text="corpus message two",
        ),
    ),
}
# A send the daemon refuses before any provider request: it captures the provider host it would
# have reached and records an empty capture.
REFUSED_SEND_CASE = "comms_send_missing_channel"
EMPTY_CAPTURE_CASES: dict[str, tuple[str, ...]] = {REFUSED_SEND_CASE: ("slack.com",)}
# The sends that persist one outbound message row, with the content that row carries.
PERSISTED_SENDS: dict[str, str] = {
    "comms_send": "corpus message one",
    "comms_attachment": "corpus caption",
    "comms_mcp_send_message": "corpus message two",
}
MESSAGES_TABLE = "comms_messages"
EMPTY_DELTA: dict[str, list[Any]] = {"added": [], "removed": [], "changed": []}
# The channel list answers and where each one holds its channel rows. Row order is not a contract.
CHANNEL_LIST_ROWS: dict[str, tuple[str, ...]] = {
    "comms_channels_list": ("body",),
    "comms_mcp_list_channels": ("result", "channels"),
}
REPLAY_WINDOW = Window(start=datetime.now(UTC), end=datetime.now(UTC))
VOICE_STATUS_CASES = ("voice_status", "voice_status_want_tts")
VOICE_TRANSCRIBE_CASE = "voice_transcribe_missing_file"
# Voice status masks address the HTTP response body.
VOICE_BODY_PREFIX = "/response/body/"
ENGINE_FIELDS = ("whisper_model", "tts_provider")
ENGINE_SUFFIXES = frozenset({"engine", "model", "package", "provider"})
AVAILABILITY_FIELDS = (
    "stt_available",
    "stt_reason",
    "voice_ready",
    "voice_loading",
    "stt_warmup_status",
    "stt_warmup_error",
    "tts_warmup_status",
    "tts_warmup_error",
    "tts_available",
    "tts_reason",
    "tts_backend_kind",
    "tts_capabilities",
    "transcription_enabled",
    "translation_enabled",
)
VISIBLE_VOICE_FIELDS = frozenset({"enabled", "stt_enabled", "tts_enabled"})
VOICE_ENABLED_CONFIG = {"voice.enabled": True, "voice.stt_enabled": True}
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


def _replay_with_body(case: dict[str, Any], field: str, value: Any) -> str:
    """Classify a replay whose response body differs from the recording in one field."""
    actual = copy.deepcopy(case["response"])
    actual["body"][field] = value
    observed: dict[str, list[Any]] = {"db_delta": [], "side_effects": []}
    return classify_replay(case, actual, {}, observed=observed).outcome


@pytest.mark.unit
def test_voice_startup_enables_voice_and_speech_to_text() -> None:
    startup = corpus_v3.load_manifest(V3_DIR).get("startup", {})
    voice = startup.get("voice")
    assert isinstance(voice, dict), "the manifest has no voice startup block"
    config = voice.get("config", {})
    for key, value in VOICE_ENABLED_CONFIG.items():
        assert config.get(key) is value, f"the voice startup block does not set {key} to {value}"


@pytest.mark.unit
def test_voice_status_cases_mask_engine_and_availability_fields() -> None:
    for name in VOICE_STATUS_CASES:
        case = _v3_case_named(name)
        assert case["credential"] == "grant", f"{name} does not present a runtime grant"
        assert case["response"]["status"] == 200, f"{name} answered {case['response']['status']}"
        body = case["response"]["body"]
        assert isinstance(body, dict), f"{name} does not answer a JSON object"
        assert body.get("enabled") is True, f"{name} was not recorded with voice enabled"
        masked = set(case["mask"])
        for field in (*ENGINE_FIELDS, *AVAILABILITY_FIELDS):
            assert f"{VOICE_BODY_PREFIX}{field}" in masked, f"{name} does not mask {field}"
        for key in body:
            pointer = f"{VOICE_BODY_PREFIX}{key}"
            if key.rsplit("_", 1)[-1] in ENGINE_SUFFIXES:
                assert pointer in masked, f"{name} does not mask engine field {key}"
            assert pointer in masked or key in VISIBLE_VOICE_FIELDS, (
                f"{name} answers {key!r}, which is neither masked nor a visible flag"
            )


@pytest.mark.unit
def test_voice_status_masks_hide_engine_values_and_keep_the_enabled_flags() -> None:
    for name in VOICE_STATUS_CASES:
        case = _v3_case_named(name)
        for field in ENGINE_FIELDS:
            outcome = _replay_with_body(case, field, "corpus-other-engine")
            assert outcome == PASSED, f"{name}: a changed {field} value is a difference"
        for field in VISIBLE_VOICE_FIELDS:
            flipped = not case["response"]["body"][field]
            outcome = _replay_with_body(case, field, flipped)
            assert outcome == DIFF, f"{name}: a changed {field} flag still matches"


@pytest.mark.unit
def test_voice_transcribe_answers_the_missing_file_envelope() -> None:
    case = _v3_case_named(VOICE_TRANSCRIBE_CASE)
    assert case["credential"] == "grant", "the transcribe case does not present a runtime grant"
    response = case["response"]
    assert response["status"] == 422, f"the transcribe case answered {response['status']}"
    detail = response["body"].get("detail")
    assert isinstance(detail, list) and detail, f"the transcribe case answered {response['body']}"
    missing = [item for item in detail if item.get("loc") == ["body", "file"]]
    assert missing, f"the transcribe case does not name the missing file: {detail}"
    assert missing[0].get("type") == "missing", f"the file error is not missing: {missing[0]}"


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
        if name in EMPTY_CAPTURE_CASES:
            # A refused send declares the host it would reach and records no request; its own
            # test checks both.
            continue
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


# Replayed provider-edge observations go through the recorder's own capture and the replay
# classifier, so each proof below exercises the comparison a replay uses.


class _ArrivalLog:
    """A provider edge arrival log holding a fixed list of arrivals."""

    def __init__(self, arrivals: list[dict[str, Any]]) -> None:
        self._arrivals = copy.deepcopy(arrivals)

    def hits(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._arrivals)

    def clear(self) -> None:
        self._arrivals.clear()


def _recorded_edge_hits(case: dict[str, Any]) -> list[Any]:
    """Every recorded provider_edge capture item of a case, in declaration and arrival order."""
    return [item for effect in _provider_edge_effects(case) for item in effect.get("recorded", [])]


def _edge_observation(case: dict[str, Any], arrivals: list[dict[str, Any]]) -> list[Any]:
    """The side-effect values a replay observes when the provider edge logged ``arrivals``."""
    observed: list[Any] = []
    for effect in case.get("side_effects", []):
        assert effect.get("channel") == "provider_edge", (
            f"{case['name']} declares a {effect.get('channel')} side effect"
        )
        items = ProviderEdgeCapture(_ArrivalLog(arrivals), _effect_hosts(effect)).hits()
        observed.append(
            normalize_captures(items, effect.get("normalizers", {}), window=REPLAY_WINDOW)
        )
    return observed


def _recorded_db(case: dict[str, Any]) -> list[Any]:
    return [copy.deepcopy(entry.get("recorded")) for entry in case.get("db_delta", [])]


def _classify_edge(
    case: dict[str, Any], arrivals: list[dict[str, Any]], db_delta: list[Any] | None = None
) -> str:
    """Classify a replay that answered the recorded reply while the edge logged ``arrivals``."""
    observed = {
        "db_delta": _recorded_db(case) if db_delta is None else db_delta,
        "side_effects": _edge_observation(case, arrivals),
    }
    return classify_replay(case, copy.deepcopy(case["response"]), {}, observed=observed).outcome


def _pinned_sequence(name: str) -> list[dict[str, Any]]:
    return copy.deepcopy(list(PROVIDER_SEQUENCES[name]))


def _messages_entries(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in case.get("db_delta", []) if entry.get("table") == MESSAGES_TABLE]


@pytest.mark.unit
def test_provider_reaching_cases_record_the_exact_provider_sequence() -> None:
    assert set(PROVIDER_SEQUENCES) == set(PROVIDER_REACHING_HOSTS), (
        "every provider-reaching case pins its provider sequence"
    )
    for name in sorted(PROVIDER_SEQUENCES):
        recorded = _recorded_edge_hits(_v3_case_named(name))
        expected = _pinned_sequence(name)
        assert recorded == expected, (
            f"{name} records the provider sequence {recorded}, expected {expected}"
        )


def _wrong_endpoint(index: int, path: str) -> Callable[[list[dict[str, Any]]], None]:
    def mutate(hits: list[dict[str, Any]]) -> None:
        hits[index]["path"] = path

    return mutate


def _drop_step(index: int) -> Callable[[list[dict[str, Any]]], None]:
    def mutate(hits: list[dict[str, Any]]) -> None:
        del hits[index]

    return mutate


def _repeat_step(index: int) -> Callable[[list[dict[str, Any]]], None]:
    def mutate(hits: list[dict[str, Any]]) -> None:
        hits.insert(index, copy.deepcopy(hits[index]))

    return mutate


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        pytest.param("comms_send", _wrong_endpoint(0, "/api/auth.test"), id="send-wrong-endpoint"),
        pytest.param("comms_send", _repeat_step(0), id="send-repeated-step"),
        pytest.param(
            "comms_attachment",
            _wrong_endpoint(2, "/api/files.completeUpload"),
            id="attachment-wrong-endpoint",
        ),
        pytest.param("comms_attachment", _drop_step(2), id="attachment-missing-step"),
        pytest.param(
            "comms_channel_create", _wrong_endpoint(0, "/api/chat.postMessage"), id="create"
        ),
    ],
)
def test_a_changed_provider_sequence_is_a_difference(
    name: str, mutation: Callable[[list[dict[str, Any]]], None]
) -> None:
    case = _v3_case_named(name)
    expected = _pinned_sequence(name)
    assert _recorded_edge_hits(case) == expected, f"{name} does not record its pinned sequence"
    assert _classify_edge(case, expected) == PASSED, f"{name}: the pinned sequence is a difference"
    mutated = _pinned_sequence(name)
    mutation(mutated)
    assert _classify_edge(case, mutated) == DIFF, f"{name}: the sequence {mutated} still matches"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "index", "field", "value"),
    [
        pytest.param("comms_send", 0, "destination", "C0OTHER0001", id="send-destination"),
        pytest.param("comms_send", 0, "text", "another text", id="send-text"),
        pytest.param(
            "comms_mcp_send_message", 0, "destination", "C0OTHER0001", id="mcp-send-destination"
        ),
        pytest.param("comms_mcp_send_message", 0, "text", "another text", id="mcp-send-text"),
        pytest.param("comms_attachment", 0, "filename", "other.txt", id="attachment-filename"),
        pytest.param("comms_attachment", 1, "payload_sha256", "f" * 64, id="attachment-bytes"),
        pytest.param(
            "comms_attachment", 2, "destination", "C0OTHER0001", id="attachment-destination"
        ),
        pytest.param("comms_attachment", 2, "text", "another caption", id="attachment-caption"),
    ],
)
def test_a_changed_outbound_payload_with_the_same_reply_is_a_difference(
    name: str, index: int, field: str, value: str
) -> None:
    case = _v3_case_named(name)
    faithful = _pinned_sequence(name)
    assert _classify_edge(case, faithful) == PASSED, f"{name}: the pinned sequence is a difference"
    mutated = _pinned_sequence(name)
    mutated[index]["semantic"][field] = value
    assert _classify_edge(case, mutated) == DIFF, (
        f"{name}: a request {index} sending {field}={value!r} with the same reply still matches"
    )


@pytest.mark.unit
@pytest.mark.parametrize(("name", "content"), sorted(PERSISTED_SENDS.items()))
def test_persisted_sends_record_one_outbound_message_row(name: str, content: str) -> None:
    case = _v3_case_named(name)
    entries = _messages_entries(case)
    assert len(entries) == 1, f"{name} declares {len(entries)} {MESSAGES_TABLE} deltas, expected 1"
    recorded = entries[0].get("recorded", {})
    assert recorded.get("removed") == [] and recorded.get("changed") == [], (
        f"{name} records a removed or changed message row: {recorded}"
    )
    added = recorded.get("added", [])
    assert len(added) == 1, f"{name} records {len(added)} added message rows, expected 1"
    assert added[0].get("direction") == "outbound", f"{name} records the row {added[0]}"
    assert added[0].get("content") == content, f"{name} records the row {added[0]}"
    unwritten = _recorded_db(case)
    unwritten[case["db_delta"].index(entries[0])] = copy.deepcopy(EMPTY_DELTA)
    sequence = _pinned_sequence(name)
    assert _classify_edge(case, sequence) == PASSED, f"{name}: the recording is a difference"
    assert _classify_edge(case, sequence, db_delta=unwritten) == DIFF, (
        f"{name}: a replay that writes no message row still matches"
    )


@pytest.mark.unit
def test_a_refused_send_records_no_provider_request_and_no_message_row() -> None:
    case = _v3_case_named(REFUSED_SEND_CASE)
    effects = _provider_edge_effects(case)
    hosts = [list(host) for host in EMPTY_CAPTURE_CASES.values()]
    assert [_effect_hosts(effect) for effect in effects] == hosts, (
        f"{REFUSED_SEND_CASE} declares the provider_edge side effects {effects}"
    )
    assert [effect.get("recorded") for effect in effects] == [[]], (
        f"{REFUSED_SEND_CASE} records provider requests: {effects}"
    )
    entries = _messages_entries(case)
    assert [entry.get("recorded") for entry in entries] == [EMPTY_DELTA], (
        f"{REFUSED_SEND_CASE} records the {MESSAGES_TABLE} deltas {entries}"
    )
    assert _classify_edge(case, []) == PASSED, "the refused send's recording is a difference"
    sent = _pinned_sequence("comms_send")
    assert _classify_edge(case, sent) == DIFF, "a refused send that reached Slack still matches"
    written = copy.deepcopy(EMPTY_DELTA)
    written["added"].append({"direction": "outbound", "content": "corpus message missing"})
    assert _classify_edge(case, [], db_delta=[written]) == DIFF, (
        "a refused send that wrote a message row still matches"
    )


def _channel_rows(document: Any, case_name: str) -> list[dict[str, Any]]:
    node = document
    for token in CHANNEL_LIST_ROWS[case_name]:
        node = node[token]
    assert isinstance(node, list), f"{case_name} answers no channel list"
    return node


def _raw_channel_answer(case: dict[str, Any], *, reverse: bool, rename: bool) -> Any:
    """A raw answer to the channel list request, with the ordinal channel id made concrete."""
    raw = copy.deepcopy(case["response"])
    rows = _channel_rows(raw, case["name"])
    for row in rows:
        if row.get("id") == "@uuid:1@":
            row["id"] = str(uuid.uuid4())
    if rename:
        rows[0]["name"] = "corpus-renamed"
    if reverse:
        rows.reverse()
    return raw


def _classify_answer(case: dict[str, Any], raw: Any) -> str:
    actual = recorder_v3._normalize_response(case, raw, REPLAY_WINDOW)
    observed: dict[str, list[Any]] = {"db_delta": [], "side_effects": []}
    return classify_replay(case, actual, {}, observed=observed).outcome


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CHANNEL_LIST_ROWS))
def test_channel_lists_match_in_any_row_order_and_differ_on_a_changed_channel(name: str) -> None:
    case = _v3_case_named(name)
    rows = _channel_rows(case["response"], name)
    assert len(rows) >= 2, f"{name} answers {len(rows)} channels, too few to reorder"
    as_recorded = _raw_channel_answer(case, reverse=False, rename=False)
    assert _classify_answer(case, as_recorded) == PASSED, f"{name}: the recorded order differs"
    reversed_rows = _raw_channel_answer(case, reverse=True, rename=False)
    assert _classify_answer(case, reversed_rows) == PASSED, (
        f"{name}: the same channels in reversed order are a difference"
    )
    renamed = _raw_channel_answer(case, reverse=True, rename=True)
    assert _classify_answer(case, renamed) == DIFF, f"{name}: a renamed channel still matches"


# A provider request whose payload the edge cannot decode is still an arrival. These requests go
# through the real edge: a CONNECT tunnel to the provider host, then raw HTTP over TLS.

RAW_BODY_TOKEN = "corpus-raw-body-token"
MALFORMED_PATH = "/api/chat.postMessage"
MALFORMED_REQUESTS: dict[str, tuple[str, bytes]] = {
    "truncated-json": (
        "Content-Type: application/json",
        b'{"channel": "C0CORPUS0001", "text": "' + RAW_BODY_TOKEN.encode() + b'"',
    ),
    "form-not-utf8": (
        "Content-Type: application/x-www-form-urlencoded",
        b"channel=C0CORPUS0001&text=" + RAW_BODY_TOKEN.encode() + b"\xff",
    ),
}


def _malformed_request(name: str) -> bytes:
    """The raw bytes of one malformed Slack POST, which asks the edge to close after it."""
    if name == "bad-chunk-size":
        head = "Transfer-Encoding: chunked"
        body = b"zz\r\n" + RAW_BODY_TOKEN.encode() + b"\r\n0\r\n\r\n"
        sized = ""
    else:
        head, body = MALFORMED_REQUESTS[name]
        sized = f"Content-Length: {len(body)}\r\n"
    lines = (
        f"POST {MALFORMED_PATH} HTTP/1.1\r\nHost: slack.com\r\n{head}\r\n{sized}"
        "Connection: close\r\n\r\n"
    )
    return lines.encode("latin-1") + body


def _send_through_edge(edge: FakeEdge, host: str, request: bytes) -> None:
    """Tunnel ``request`` to ``host`` through the edge and read until the edge closes the tunnel."""
    context = ssl.create_default_context(cafile=str(edge.ca_path()))
    with socket.create_connection(edge.address, timeout=10.0) as conn:
        conn.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode("latin-1"))
        established = b""
        while b"\r\n\r\n" not in established:
            chunk = conn.recv(4096)
            assert chunk, f"the edge closed the CONNECT to {host} before answering"
            established += chunk
        assert established.startswith(b"HTTP/1.1 200"), f"the edge answered {established!r}"
        with context.wrap_socket(conn, server_hostname=host) as tls:
            tls.sendall(request)
            try:
                while tls.recv(4096):
                    pass
            except (OSError, ssl.SSLError):
                pass


@pytest.mark.integration
@pytest.mark.parametrize("name", [*sorted(MALFORMED_REQUESTS), "bad-chunk-size"])
def test_a_malformed_provider_request_is_captured_by_its_signature(
    name: str, tmp_path: Path
) -> None:
    with _started_edge(tmp_path) as edge:
        _send_through_edge(edge, "slack.com", _malformed_request(name))
        arrivals = edge.hits()
        captured = ProviderEdgeCapture(edge, ["slack.com"]).hits()
    signatures = [(item["host"], item["method"], item["path"]) for item in captured]
    assert signatures == [("slack.com", "POST", MALFORMED_PATH)], (
        f"{name}: the malformed request was captured as {captured}"
    )
    assert captured[0] != _hit("slack.com", "POST", MALFORMED_PATH), (
        f"{name}: the malformed request is captured as a well-formed request sending nothing"
    )
    marker = arrivals[0].get(DECODE_ERROR_KEY)
    assert isinstance(marker, str) and marker, (
        f"{name}: the arrival {arrivals[0]} carries no {DECODE_ERROR_KEY} marker"
    )
    assert RAW_BODY_TOKEN not in json.dumps(arrivals), f"{name}: the edge log keeps the raw body"


@pytest.mark.integration
def test_a_refused_send_that_made_a_malformed_provider_request_is_a_difference(
    tmp_path: Path,
) -> None:
    case = _v3_case_named(REFUSED_SEND_CASE)
    with _started_edge(tmp_path) as edge:
        observed: list[Any] = []
        for effect in _provider_edge_effects(case):
            with ProviderEdgeCapture(edge, _effect_hosts(effect)) as capture:
                _send_through_edge(edge, "slack.com", _malformed_request("truncated-json"))
                items = capture.hits()
            observed.append(
                normalize_captures(items, effect.get("normalizers", {}), window=REPLAY_WINDOW)
            )
    outcome = classify_replay(
        case,
        copy.deepcopy(case["response"]),
        {},
        observed={"db_delta": _recorded_db(case), "side_effects": observed},
    ).outcome
    assert outcome == DIFF, (
        f"a refused send that made a malformed Slack request classifies {outcome}: {observed}"
    )
