"""Plan-accept option registry tests.

The option set is uniform across every source: two approve actions (YOLO /
Act) whose ``post_plan_chat_mode`` carries the chosen execution mode. Reject is
a separate request-changes action handled outside this registry.
"""

from __future__ import annotations

import pytest

from gobby.adapters.plan_options import (
    PlanAcceptOption,
    get_plan_accept_option,
    get_plan_accept_options,
    serialize_plan_accept_options,
)
from gobby.hooks.events import SessionSource

pytestmark = pytest.mark.unit

# Every source resolves to the same fixed set, including bare strings, the
# ``<provider>_web_chat`` form, and unknown sources.
_SOURCES = [
    SessionSource.CLAUDE,
    SessionSource.CODEX,
    SessionSource.GEMINI,
    SessionSource.QWEN,
    SessionSource.GROK,
    SessionSource.DROID,
    SessionSource.AGY,
    SessionSource.PIPELINE,
    "droid",
    "droid_web_chat",
    "totally-unknown-cli",
]


@pytest.mark.parametrize("source", _SOURCES)
def test_every_source_yields_the_fixed_yolo_act_set(source: SessionSource | str) -> None:
    options = get_plan_accept_options(source)
    assert [opt.id for opt in options] == ["approve_yolo", "approve_act"]


def test_yolo_is_the_dominant_bypass_primary() -> None:
    yolo = get_plan_accept_options(SessionSource.CLAUDE)[0]
    assert yolo.id == "approve_yolo"
    assert yolo.decision == "approve"
    assert yolo.post_plan_chat_mode == "bypass"
    assert yolo.emphasis == "primary"
    assert yolo.auto_continue is True


def test_act_is_the_quieter_normal_accent() -> None:
    act = get_plan_accept_options(SessionSource.CLAUDE)[1]
    assert act.id == "approve_act"
    assert act.decision == "approve"
    assert act.post_plan_chat_mode == "normal"
    assert act.emphasis == "accent"
    assert act.auto_continue is True


@pytest.mark.parametrize("source", _SOURCES)
def test_first_option_is_an_approve_continue_default(source: SessionSource | str) -> None:
    """The first entry is always an approve+continue default for graceful UI."""
    first = get_plan_accept_options(source)[0]
    assert first.decision == "approve"
    assert first.auto_continue is True
    assert first.post_plan_chat_mode != "plan"


@pytest.mark.parametrize("source", _SOURCES)
def test_option_ids_are_unique_and_modes_valid(source: SessionSource | str) -> None:
    options = get_plan_accept_options(source)
    ids = [opt.id for opt in options]
    assert len(ids) == len(set(ids)), f"duplicate option ids: {ids}"
    for opt in options:
        assert opt.decision == "approve"
        assert opt.post_plan_chat_mode in {"normal", "bypass"}
        assert opt.emphasis in {"primary", "accent"}


def test_all_sources_share_the_same_set() -> None:
    claude = get_plan_accept_options(SessionSource.CLAUDE)
    for source in _SOURCES:
        assert get_plan_accept_options(source) == claude


def test_string_and_web_chat_suffix_sources_resolve() -> None:
    """Accepts bare provider strings and the ``<provider>_web_chat`` form."""
    bare = get_plan_accept_options("droid")
    suffixed = get_plan_accept_options("droid_web_chat")
    assert bare == suffixed == get_plan_accept_options(SessionSource.DROID)


def test_get_plan_accept_option_by_id() -> None:
    opt = get_plan_accept_option(SessionSource.CLAUDE, "approve_yolo")
    assert isinstance(opt, PlanAcceptOption)
    assert opt.post_plan_chat_mode == "bypass"
    act = get_plan_accept_option(SessionSource.DROID, "approve_act")
    assert isinstance(act, PlanAcceptOption)
    assert act.post_plan_chat_mode == "normal"


def test_get_plan_accept_option_missing_returns_none() -> None:
    assert get_plan_accept_option(SessionSource.CLAUDE, None) is None
    assert get_plan_accept_option(SessionSource.CLAUDE, "") is None
    assert get_plan_accept_option(SessionSource.CLAUDE, "does_not_exist") is None
    # Retired legacy ids no longer resolve.
    assert get_plan_accept_option(SessionSource.CLAUDE, "ultraplan") is None
    assert get_plan_accept_option(SessionSource.CLAUDE, "keep_planning") is None


def test_serialize_exposes_only_frontend_fields() -> None:
    serialized = serialize_plan_accept_options(SessionSource.DROID)
    assert serialized  # non-empty
    for entry in serialized:
        assert set(entry.keys()) == {"id", "label", "description", "decision", "emphasis"}
        # Server-side action primitives are never sent to the client.
        assert "post_plan_chat_mode" not in entry
        assert "auto_continue" not in entry
        assert "clear_context" not in entry
    # Emphasis carries button hierarchy: YOLO primary, Act accent.
    by_id = {entry["id"]: entry for entry in serialized}
    assert by_id["approve_yolo"]["emphasis"] == "primary"
    assert by_id["approve_act"]["emphasis"] == "accent"


def test_plan_accept_option_is_frozen() -> None:
    opt = get_plan_accept_options(SessionSource.CLAUDE)[0]
    with pytest.raises((AttributeError, TypeError)):
        opt.id = "mutated"  # type: ignore[misc]
