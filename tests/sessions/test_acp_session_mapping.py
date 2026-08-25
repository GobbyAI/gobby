"""Tests for the ACP -> Gobby translation seam (``acp_session_mapping``)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from gobby.sessions.acp_session_mapping import (
    ACP_CLOSE_STATUS,
    ACP_DELETE_DISPOSITION,
    SESSION_TYPE_WEB_CHAT,
    build_acp_block,
    disposition_for_delete,
    map_session_info,
    normalize_additional_directories,
    status_for_close,
)

pytestmark = pytest.mark.unit


def _resolver(mapping: dict[str, str]) -> Callable[[str | None], str | None]:
    def resolve(cwd: str | None) -> str | None:
        return mapping.get(cwd) if cwd else None

    return resolve


def test_map_session_info_translates_to_canonical_fields() -> None:
    info = {
        "sessionId": "sess-123",
        "cwd": "/repo",
        "title": "ACP title",
        "updatedAt": "2026-06-27T05:00:00Z",
        "additionalDirectories": ["/repo/pkg", "/repo/docs", 42],
    }

    mapped = map_session_info(
        info,
        provider="grok",
        resolve_project_id=_resolver({"/repo": "proj-1"}),
    )

    assert mapped is not None
    assert mapped.external_id == "sess-123"
    assert mapped.source == "grok"
    assert mapped.title == "ACP title"
    assert mapped.project_id == "proj-1"
    assert mapped.session_type == SESSION_TYPE_WEB_CHAT
    assert mapped.cwd == "/repo"
    assert mapped.updated_at == "2026-06-27T05:00:00Z"
    # camelCase additionalDirectories normalized to a clean snake-cased tuple
    assert mapped.additional_directories == ("/repo/pkg", "/repo/docs")


def test_map_session_info_unresolved_cwd_yields_none_project() -> None:
    mapped = map_session_info(
        {"sessionId": "s1", "cwd": "/unknown"},
        provider="qwen",
        resolve_project_id=_resolver({}),
    )

    assert mapped is not None
    assert mapped.project_id is None


def test_map_session_info_strips_strings_and_drops_empty_values() -> None:
    mapped = map_session_info(
        {
            "sessionId": "  sess-123  ",
            "cwd": "  /repo  ",
            "title": "   ",
            "updatedAt": "  ",
        },
        provider="qwen",
        resolve_project_id=_resolver({"/repo": "proj-1"}),
    )

    assert mapped is not None
    assert mapped.external_id == "sess-123"
    assert mapped.cwd == "/repo"
    assert mapped.title is None
    assert mapped.updated_at is None
    assert mapped.project_id == "proj-1"


def test_map_session_info_requires_session_id() -> None:
    assert (
        map_session_info({"cwd": "/repo"}, provider="grok", resolve_project_id=_resolver({}))
        is None
    )
    assert (
        map_session_info(
            "not-a-mapping",
            provider="grok",
            resolve_project_id=_resolver({}),
        )
        is None
    )


def test_build_acp_block_exposes_only_gated_capabilities_snake_case() -> None:
    block = build_acp_block(
        {"list": True, "resume": True, "close": False, "delete": True},
        additional_directories=["/repo/extra", "", 7],
    )

    assert block == {
        "capabilities": {"resume": True, "close": False, "delete": True},
        "additional_directories": ["/repo/extra"],
    }
    # backend-internal ``list`` capability never crosses the seam
    assert "list" not in block["capabilities"]
    # no camelCase keys anywhere in the normalized block
    assert _no_camel_case_keys(block)


def test_build_acp_block_defaults_to_empty() -> None:
    block = build_acp_block({})

    assert block == {
        "capabilities": {"resume": False, "close": False, "delete": False},
        "additional_directories": [],
    }


def test_normalize_additional_directories_filters_non_strings() -> None:
    assert normalize_additional_directories(["/a", "", None, "/b", 3]) == ("/a", "/b")
    assert normalize_additional_directories(None) == ()
    assert normalize_additional_directories("not-a-list") == ()


def test_lifecycle_outcome_helpers() -> None:
    assert status_for_close() == ACP_CLOSE_STATUS == "expired"
    assert disposition_for_delete() == ACP_DELETE_DISPOSITION == "removed"


def _no_camel_case_keys(value: object) -> bool:
    """Recursively assert no dict key contains an uppercase letter."""
    if isinstance(value, dict):
        return all(key == key.lower() and _no_camel_case_keys(sub) for key, sub in value.items())
    if isinstance(value, (list, tuple)):
        return all(_no_camel_case_keys(item) for item in value)
    return True
