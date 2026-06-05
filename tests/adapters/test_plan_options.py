"""Per-CLI plan-accept option registry tests."""

from __future__ import annotations

import pytest

from gobby.adapters.plan_options import (
    PLAN_ACCEPT_OPTIONS,
    PlanAcceptOption,
    get_plan_accept_option,
    get_plan_accept_options,
    serialize_plan_accept_options,
)
from gobby.hooks.events import SessionSource

pytestmark = pytest.mark.unit

# Provider sources that carry a bespoke, user-presented option set.
_BESPOKE_SOURCES = [
    SessionSource.CLAUDE,
    SessionSource.CODEX,
    SessionSource.GEMINI,
    SessionSource.QWEN,
    SessionSource.GROK,
    SessionSource.DROID,
]


@pytest.mark.parametrize("source", _BESPOKE_SOURCES)
def test_every_bespoke_source_has_options(source: SessionSource) -> None:
    options = get_plan_accept_options(source)
    assert options, f"{source} must expose at least one plan-accept option"


@pytest.mark.parametrize("source", _BESPOKE_SOURCES)
def test_first_option_is_canonical_approve_continue(source: SessionSource) -> None:
    """The first entry is always an approve+continue default for graceful UI."""
    first = get_plan_accept_options(source)[0]
    assert first.decision == "approve"
    assert first.auto_continue is True
    assert first.post_plan_chat_mode != "plan"


@pytest.mark.parametrize("source", list(PLAN_ACCEPT_OPTIONS))
def test_option_ids_are_unique_per_source(source: SessionSource) -> None:
    options = get_plan_accept_options(source)
    ids = [opt.id for opt in options]
    assert len(ids) == len(set(ids)), f"duplicate option ids in {source}: {ids}"


@pytest.mark.parametrize("source", list(PLAN_ACCEPT_OPTIONS))
def test_decision_and_mode_enums_are_valid(source: SessionSource) -> None:
    for opt in get_plan_accept_options(source):
        assert opt.decision in {"approve", "keep_planning"}
        assert opt.post_plan_chat_mode in {"plan", "normal", "accept_edits", "bypass"}
        # keep_planning never leaves plan mode and never auto-continues.
        if opt.decision == "keep_planning":
            assert opt.post_plan_chat_mode == "plan"
            assert opt.auto_continue is False
        # approve always leaves plan mode.
        if opt.decision == "approve":
            assert opt.post_plan_chat_mode != "plan"


def test_claude_option_set_matches_contract() -> None:
    by_id = {opt.id: opt for opt in get_plan_accept_options(SessionSource.CLAUDE)}
    assert by_id["approve_manual"].post_plan_chat_mode == "normal"
    assert by_id["approve_accept_edits"].post_plan_chat_mode == "accept_edits"
    assert by_id["approve_bypass"].post_plan_chat_mode == "bypass"
    ultraplan = by_id["ultraplan"]
    assert ultraplan.decision == "keep_planning"
    assert ultraplan.escalate is True
    assert ultraplan.auto_continue is False


def test_codex_clear_context_option() -> None:
    by_id = {opt.id: opt for opt in get_plan_accept_options(SessionSource.CODEX)}
    plain = by_id["approve"]
    clear = by_id["approve_clear_context"]
    assert plain.clear_context is False
    assert clear.clear_context is True
    assert clear.decision == "approve"
    assert clear.auto_continue is True


def test_acp_sources_share_approval_mode_set() -> None:
    gemini = get_plan_accept_options(SessionSource.GEMINI)
    for source in (SessionSource.QWEN, SessionSource.GROK, SessionSource.DROID):
        assert get_plan_accept_options(source) == gemini
    ids = {opt.id for opt in gemini}
    assert {"approve_default", "approve_auto_edit", "approve_yolo", "keep_planning"} <= ids


def test_get_plan_accept_option_by_id() -> None:
    opt = get_plan_accept_option(SessionSource.CLAUDE, "approve_bypass")
    assert isinstance(opt, PlanAcceptOption)
    assert opt.post_plan_chat_mode == "bypass"


def test_get_plan_accept_option_missing_returns_none() -> None:
    assert get_plan_accept_option(SessionSource.CLAUDE, None) is None
    assert get_plan_accept_option(SessionSource.CLAUDE, "") is None
    assert get_plan_accept_option(SessionSource.CLAUDE, "does_not_exist") is None


def test_string_and_web_chat_suffix_sources_resolve() -> None:
    """Accepts bare provider strings and the ``<provider>_web_chat`` form."""
    bare = get_plan_accept_options("droid")
    suffixed = get_plan_accept_options("droid_web_chat")
    assert bare == suffixed == get_plan_accept_options(SessionSource.DROID)


def test_unknown_source_degrades_to_generic_approve() -> None:
    options = get_plan_accept_options("totally-unknown-cli")
    assert len(options) == 1
    only = options[0]
    assert only.id == "approve"
    assert only.decision == "approve"
    assert only.auto_continue is True


def test_serialize_exposes_only_frontend_fields() -> None:
    serialized = serialize_plan_accept_options(SessionSource.CODEX)
    assert serialized  # non-empty
    for entry in serialized:
        assert set(entry.keys()) == {"id", "label", "description", "decision"}
        # Server-side action primitives are never sent to the client.
        assert "post_plan_chat_mode" not in entry
        assert "auto_continue" not in entry
        assert "clear_context" not in entry


def test_plan_accept_option_is_frozen() -> None:
    opt = get_plan_accept_options(SessionSource.CLAUDE)[0]
    with pytest.raises((AttributeError, TypeError)):
        opt.id = "mutated"  # type: ignore[misc]
