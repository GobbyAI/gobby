"""Tests for gobby.utils.session_context.

Covers:
- resolve_and_seed_contexts (override vs. fallback modes, minimal fallback,
  session unresolvable, ambiguous external_id warning).
- reset_seeded_contexts on empty / partial tokens.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import (
    SeededContextTokens,
    get_session_context,
    reset_seeded_contexts,
    resolve_and_seed_contexts,
)

pytestmark = pytest.mark.unit


# --- Fixtures / helpers ----------------------------------------------------


SESSION_PLATFORM_UUID = str(uuid.uuid4())
SESSION_EXTERNAL_UUID = str(uuid.uuid4())
PROJECT_A_UUID = str(uuid.uuid4())
PROJECT_B_UUID = str(uuid.uuid4())


def _make_session_manager(
    *,
    resolve_to: str | None = SESSION_PLATFORM_UUID,
    resolve_exc: Exception | None = None,
    external_id: str | None = SESSION_EXTERNAL_UUID,
    project_id: str | None = PROJECT_A_UUID,
) -> MagicMock:
    mgr = MagicMock()
    mgr.db = MagicMock()
    if resolve_exc is not None:
        mgr.resolve_session_reference.side_effect = resolve_exc
    else:
        mgr.resolve_session_reference.return_value = resolve_to
    session = MagicMock()
    session.external_id = external_id
    session.project_id = project_id
    mgr.get.return_value = session
    return mgr


# --- Core resolution -------------------------------------------------------


def test_resolve_and_seed_contexts_external_id_ref_resolves_to_platform_uuid() -> None:
    """Primary regression: an external_id UUID becomes the platform id in SessionContext."""
    mgr = _make_session_manager(resolve_to=SESSION_PLATFORM_UUID)
    with patch(
        "gobby.utils.project_context.set_project_context_from_session",
        return_value="project-token",
    ):
        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref=None,
            db=mgr.db,
        )
    try:
        assert tokens.resolved_session_id == SESSION_PLATFORM_UUID
        ctx = get_session_context()
        assert ctx is not None
        assert ctx.session_id == SESSION_PLATFORM_UUID
        assert ctx.conversation_id == SESSION_EXTERNAL_UUID
    finally:
        reset_seeded_contexts(tokens)
    mgr.resolve_session_reference.assert_called_once_with(SESSION_EXTERNAL_UUID, None)


def test_resolve_and_seed_contexts_session_only_derives_project_from_session() -> None:
    """project_ref=None + session resolves → project context derived from session."""
    mgr = _make_session_manager()
    with patch(
        "gobby.utils.project_context.set_project_context_from_session",
        return_value="project-token",
    ) as mock_from_session:
        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref=None,
            db=mgr.db,
        )
    try:
        assert tokens.project_token == "project-token"
        mock_from_session.assert_called_once_with(SESSION_PLATFORM_UUID, mgr, mgr.db)
    finally:
        reset_seeded_contexts(tokens)


# --- Override mode ---------------------------------------------------------


def test_override_mode_project_ref_uuid_beats_session_derived_project() -> None:
    """Explicit project_id overrides session-derived project."""
    mgr = _make_session_manager()
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            return_value="override-token",
        ) as mock_from_ref,
        patch(
            "gobby.utils.project_context.set_project_context_from_session",
        ) as mock_from_session,
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            db=mgr.db,
        )
    try:
        assert tokens.resolved_session_id == SESSION_PLATFORM_UUID
        assert tokens.resolved_project_id == PROJECT_B_UUID
        assert tokens.project_token == "override-token"
        mock_from_ref.assert_called_once_with(PROJECT_B_UUID, mgr.db)
        mock_from_session.assert_not_called()
    finally:
        reset_seeded_contexts(tokens)


def test_override_mode_project_ref_name_canonicalized() -> None:
    """project_ref='my-project' (a name) is canonicalized to a UUID before scoping."""
    mgr = _make_session_manager()
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            return_value="tok",
        ),
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref="my-project",
            db=mgr.db,
        )
    try:
        # Resolver scoped by the canonical UUID, not the name
        mgr.resolve_session_reference.assert_called_once_with(SESSION_EXTERNAL_UUID, PROJECT_B_UUID)
        assert tokens.resolved_project_id == PROJECT_B_UUID
    finally:
        reset_seeded_contexts(tokens)


def test_override_mode_project_ref_unresolvable_returns_none_project_id() -> None:
    """Helper does not raise; resolved_project_id is None — caller decides."""
    mgr = _make_session_manager()
    with patch(
        "gobby.storage.projects.LocalProjectManager",
    ) as mock_pm_class:
        mock_pm = MagicMock()
        mock_pm.resolve_ref.return_value = None
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref="nonexistent-project",
            db=mgr.db,
        )
    try:
        assert tokens.resolved_project_id is None
    finally:
        reset_seeded_contexts(tokens)


# --- Fallback mode ---------------------------------------------------------


def test_fallback_mode_session_project_wins_over_project_ref() -> None:
    """Fallback mode: session-derived project beats the project_ref hint."""
    mgr = _make_session_manager()
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_session",
            return_value="session-derived-token",
        ) as mock_from_session,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
        ) as mock_from_ref,
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            project_ref_is_fallback=True,
            db=mgr.db,
        )
    try:
        assert tokens.project_token == "session-derived-token"
        mock_from_session.assert_called_once_with(SESSION_PLATFORM_UUID, mgr, mgr.db)
        mock_from_ref.assert_not_called()
    finally:
        reset_seeded_contexts(tokens)


def test_fallback_mode_session_unresolvable_project_ref_sets_project() -> None:
    """Session unresolvable in fallback mode → project_ref seeds the project context."""
    mgr = _make_session_manager(resolve_exc=ValueError("Session not found"))
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            return_value="ref-token",
        ) as mock_from_ref,
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref="bogus",
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            project_ref_is_fallback=True,
            db=mgr.db,
        )
    try:
        assert tokens.session_token is None
        assert tokens.project_token == "ref-token"
        mock_from_ref.assert_called_once_with(PROJECT_B_UUID, mgr.db)
    finally:
        reset_seeded_contexts(tokens)


def test_fallback_mode_session_derivation_fails_falls_through_to_project_ref() -> None:
    """Session resolves but set_project_context_from_session returns None → project_ref fallback."""
    mgr = _make_session_manager()
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_session",
            return_value=None,
        ),
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            return_value="fallback-token",
        ) as mock_from_ref,
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref=SESSION_EXTERNAL_UUID,
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            project_ref_is_fallback=True,
            db=mgr.db,
        )
    try:
        assert tokens.project_token == "fallback-token"
        mock_from_ref.assert_called_once_with(PROJECT_B_UUID, mgr.db)
    finally:
        reset_seeded_contexts(tokens)


# --- Minimal / no-DB fallback ---------------------------------------------


def test_minimal_fallback_without_db_sets_id_only_project_context() -> None:
    """db=None + project_ref UUID → minimal {'id': project_ref} context."""
    tokens = resolve_and_seed_contexts(
        session_ref=None,
        session_manager=None,
        project_ref=PROJECT_B_UUID,
        db=None,
    )
    try:
        ctx = get_project_context()
        assert ctx is not None
        assert ctx.get("id") == PROJECT_B_UUID
    finally:
        reset_seeded_contexts(tokens)


def test_minimal_fallback_with_db_but_enrichment_failure() -> None:
    """db present but set_project_context_from_ref throws → minimal fallback."""
    mgr = _make_session_manager(resolve_exc=ValueError("nope"))
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            side_effect=RuntimeError("enrichment failed"),
        ),
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens = resolve_and_seed_contexts(
            session_ref="bogus",
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            db=mgr.db,
        )
    try:
        ctx = get_project_context()
        assert ctx is not None
        assert ctx.get("id") == PROJECT_B_UUID
    finally:
        reset_seeded_contexts(tokens)


# --- Other edge cases ------------------------------------------------------


def test_resolve_and_seed_contexts_both_unresolvable_returns_empty_tokens() -> None:
    """No session, no project → empty SeededContextTokens, reset is a no-op."""
    tokens = resolve_and_seed_contexts(
        session_ref=None,
        session_manager=None,
        project_ref=None,
        db=None,
    )
    assert tokens.session_token is None
    assert tokens.project_token is None
    assert tokens.resolved_session_id is None
    assert tokens.resolved_project_id is None
    reset_seeded_contexts(tokens)  # must not raise


def test_resolve_and_seed_contexts_valueerror_on_session_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ambiguous external_id surfaces as a warning and leaves session_token empty."""
    mgr = _make_session_manager(resolve_exc=ValueError("Ambiguous session reference"))
    caplog.set_level(logging.WARNING, logger="gobby.utils.session_context")
    tokens = resolve_and_seed_contexts(
        session_ref=SESSION_EXTERNAL_UUID,
        session_manager=mgr,
        project_ref=None,
        db=mgr.db,
    )
    try:
        assert tokens.session_token is None
        assert tokens.resolved_session_id is None
        assert any("could not resolve session ref" in rec.message for rec in caplog.records)
    finally:
        reset_seeded_contexts(tokens)


def test_reset_seeded_contexts_safe_on_empty_and_partial_tokens() -> None:
    """reset() must tolerate None session_token, None project_token, or both."""
    reset_seeded_contexts(SeededContextTokens())
    # Partial: only project_token set — simulate via direct construction and reset
    mgr = _make_session_manager(resolve_exc=ValueError("nope"))
    with (
        patch(
            "gobby.storage.projects.LocalProjectManager",
        ) as mock_pm_class,
        patch(
            "gobby.utils.project_context.set_project_context_from_ref",
            return_value="token",
        ),
    ):
        mock_pm = MagicMock()
        project = MagicMock()
        project.id = PROJECT_B_UUID
        mock_pm.resolve_ref.return_value = project
        mock_pm_class.return_value = mock_pm

        tokens: Any = resolve_and_seed_contexts(
            session_ref="bogus",
            session_manager=mgr,
            project_ref=PROJECT_B_UUID,
            db=mgr.db,
        )
    assert tokens.session_token is None
    assert tokens.project_token is not None
    reset_seeded_contexts(tokens)  # must not raise
