"""Tests for ACP ``sessionCapabilities`` parsing (presence-not-null semantics)."""

from __future__ import annotations

import pytest

from gobby.adapters.acp_session_state import (
    ACPSessionState,
    extract_session_infos,
    parse_session_capabilities,
)

pytestmark = pytest.mark.unit

_CAP_KEYS = ("list", "resume", "close", "delete", "additional_directories")


def test_empty_session_capabilities_means_all_unsupported() -> None:
    parsed = parse_session_capabilities({"sessionCapabilities": {}})

    assert parsed == dict.fromkeys(_CAP_KEYS, False)


def test_present_non_null_subobjects_are_supported() -> None:
    parsed = parse_session_capabilities({"sessionCapabilities": {"list": {}, "delete": {}}})

    assert parsed["list"] is True
    assert parsed["delete"] is True
    assert parsed["resume"] is False
    assert parsed["close"] is False
    assert parsed["additional_directories"] is False


def test_missing_and_null_capabilities_are_unsupported() -> None:
    missing = parse_session_capabilities({"agentCapabilities": {}})
    null_keys = parse_session_capabilities({"sessionCapabilities": {"resume": None, "close": None}})

    assert missing == dict.fromkeys(_CAP_KEYS, False)
    assert null_keys["resume"] is False
    assert null_keys["close"] is False


def test_additional_directories_camelcase_wire_key_maps_to_snake_case() -> None:
    parsed = parse_session_capabilities({"sessionCapabilities": {"additionalDirectories": {}}})

    assert parsed["additional_directories"] is True
    # camelCase must never leak past the seam
    assert "additionalDirectories" not in parsed


def test_non_dict_payload_yields_all_unsupported() -> None:
    assert parse_session_capabilities(None) == dict.fromkeys(_CAP_KEYS, False)
    assert parse_session_capabilities([1, 2, 3]) == dict.fromkeys(_CAP_KEYS, False)


def test_state_exposes_capability_properties() -> None:
    state = ACPSessionState()

    state.update_agent_capabilities({"sessionCapabilities": {"resume": {}, "close": {}}})

    assert state.supports_session_resume is True
    assert state.supports_session_close is True
    assert state.supports_session_list is False
    assert state.supports_session_delete is False
    assert state.supports_session_additional_directories is False


def test_session_capabilities_accessor_returns_snake_case_copy() -> None:
    state = ACPSessionState()
    state.update_agent_capabilities({"sessionCapabilities": {"list": {}}})

    caps = state.session_capabilities
    caps["list"] = False  # mutate the copy

    assert set(caps) == set(_CAP_KEYS)
    assert state.supports_session_list is True  # internal state unaffected


def test_load_session_is_independent_of_session_capabilities() -> None:
    state = ACPSessionState()

    state.update_agent_capabilities({"loadSession": True, "sessionCapabilities": {}})
    assert state.supports_session_load() is True
    assert state.supports_session_resume is False

    state.update_agent_capabilities({"loadSession": False, "sessionCapabilities": {"resume": {}}})
    assert state.supports_session_load() is False
    assert state.supports_session_resume is True


def test_reset_clears_session_capabilities() -> None:
    state = ACPSessionState()
    state.update_agent_capabilities({"sessionCapabilities": {"list": {}}})

    state.reset()

    assert state.session_capabilities == {}
    assert state.supports_session_list is False


def test_extract_session_infos_from_result_object() -> None:
    result = {
        "sessions": [
            {"sessionId": "s1", "cwd": "/repo"},
            "not-a-dict",
            {"sessionId": "s2"},
        ],
        "nextCursor": "abc",
    }

    infos = extract_session_infos(result)

    assert [info["sessionId"] for info in infos] == ["s1", "s2"]


def test_extract_session_infos_accepts_bare_list_and_rejects_other() -> None:
    assert extract_session_infos([{"sessionId": "s1"}]) == [{"sessionId": "s1"}]
    assert extract_session_infos({"sessions": None}) == []
    assert extract_session_infos(None) == []
