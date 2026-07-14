"""Parent lookup and cross-source recovery for session registration."""

from __future__ import annotations

import logging
import time
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
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> list[Session]: ...

    def find_parent(
        self,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "handoff_ready",
        max_age_minutes: int = 10,
    ) -> Session | None: ...

    def cache_session_mapping(
        self,
        external_id: str,
        source: str,
        session_id: str,
        machine_id: str | None = None,
        project_id: str | None = None,
    ) -> None: ...


class _RegistrationRecoveryMixin:
    def find_parent_session(
        self: _RegistrationRecoveryHost,
        machine_id: str,
        source: str,
        project_id: str,
        max_attempts: int = 30,
    ) -> tuple[str, str | None] | None:
        """Poll for a handoff-ready parent session on this machine and project."""
        attempt = 0

        while attempt < max_attempts:
            try:
                session = self.find_parent(
                    machine_id=machine_id,
                    source=source,
                    project_id=project_id,
                )

                if session:
                    self.logger.debug(
                        "Found parent session %s (attempt %s/%s)",
                        session.id,
                        attempt + 1,
                        max_attempts,
                    )
                    return (session.id, session.summary_markdown)

                attempt += 1
                if attempt < max_attempts:
                    self.logger.debug(
                        "No handoff_ready session yet, retrying in 1s (attempt %s/%s)",
                        attempt,
                        max_attempts,
                    )
                    time.sleep(1)

            except Exception as error:
                self.logger.warning(
                    "Error polling for parent session (attempt %s): %s",
                    attempt + 1,
                    error,
                )
                attempt += 1
                if attempt < max_attempts:
                    time.sleep(1)
                else:
                    self.logger.error("Exhausted retries finding parent session: %s", error)
                    return None

        self.logger.debug("No handoff_ready session found after %s attempts", max_attempts)
        return None

    def recover_session(
        self: _RegistrationRecoveryHost,
        external_id: str,
        source: str,
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> Session | None:
        """Recover an existing session across sources when lookup is unambiguous."""
        try:
            candidates = self.find_by_external_id_all_sources(
                external_id=external_id,
                machine_id=machine_id,
                project_id=project_id,
                session_type=session_type,
            )
            if not candidates:
                return None
            if project_id is None and len({candidate.project_id for candidate in candidates}) > 1:
                self.logger.warning(
                    "Ambiguous cross-project session recovery for external_id=%s source=%s "
                    "machine_id=%s candidates=%s",
                    external_id,
                    source,
                    machine_id,
                    [candidate.id for candidate in candidates],
                )
                return None

            ranked = sorted(candidates, key=_recovery_rank)
            if len(ranked) > 1 and _recovery_score(ranked[0]) == _recovery_score(ranked[1]):
                self.logger.warning(
                    "Ambiguous cross-source session recovery for external_id=%s source=%s "
                    "machine_id=%s project_id=%s candidates=%s",
                    external_id,
                    source,
                    machine_id,
                    project_id,
                    [candidate.id for candidate in ranked[:2]],
                )
                return None

            recovered = ranked[0]
            self.cache_session_mapping(
                external_id,
                source,
                recovered.id,
                machine_id=machine_id,
                project_id=project_id,
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
