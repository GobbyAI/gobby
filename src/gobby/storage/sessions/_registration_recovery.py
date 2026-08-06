"""Cross-source recovery for session registration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from gobby.storage.session_models import Session
from gobby.terminal_context import terminal_context_has_tmux_target


def _recovery_score(session: Session) -> tuple[bool, bool, bool, bool]:
    """Score recovery candidates by metadata completeness only."""
    return (
        not bool(session.transcript_path),
        not bool(session.title),
        not terminal_context_has_tmux_target(session.terminal_context),
        not bool(session.terminal_context),
    )


def _recovery_rank(session: Session) -> tuple[bool, bool, bool, bool, datetime, str]:
    """Rank cross-source recovery candidates by completeness, then age."""
    return (*_recovery_score(session), session.created_at, session.id)


class _RegistrationRecoveryHost(Protocol):
    logger: logging.Logger

    def find_by_external_id_all_sources(
        self,
        external_id: str,
        project_id: str | None,
        session_type: str | None = "terminal",
    ) -> list[Session]: ...

    def cache_session_mapping(
        self,
        external_id: str,
        source: str,
        session_id: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> None: ...


class _RegistrationRecoveryMixin:
    def recover_session(
        self: _RegistrationRecoveryHost,
        external_id: str,
        source: str,
        project_id: str | None,
        session_type: str | None = "terminal",
    ) -> Session | None:
        """Recover an existing session across sources when lookup is unambiguous."""
        try:
            candidates = self.find_by_external_id_all_sources(
                external_id=external_id,
                project_id=project_id,
                session_type=session_type,
            )
            if not candidates:
                return None
            if project_id is None and len({candidate.project_id for candidate in candidates}) > 1:
                self.logger.warning(
                    "Ambiguous cross-project session recovery for external_id=%s source=%s "
                    "candidates=%s",
                    external_id,
                    source,
                    [candidate.id for candidate in candidates],
                )
                return None

            ranked = sorted(candidates, key=_recovery_rank)
            if len(ranked) > 1 and _recovery_score(ranked[0]) == _recovery_score(ranked[1]):
                self.logger.warning(
                    "Ambiguous cross-source session recovery for external_id=%s source=%s "
                    "project_id=%s candidates=%s",
                    external_id,
                    source,
                    project_id,
                    [candidate.id for candidate in ranked[:2]],
                )
                return None

            recovered = ranked[0]
            self.cache_session_mapping(
                external_id,
                source,
                recovered.id,
                project_id=recovered.project_id,
                session_type=recovered.session_type,
            )
            return recovered

        except Exception as error:
            self.logger.debug(
                "Failed to recover session_id across sources for external_id=%s: %s",
                external_id,
                error,
                exc_info=True,
            )
            return None
