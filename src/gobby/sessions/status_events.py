"""Semantic session status transition records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from gobby.storage.session_models import Session


@dataclass(frozen=True, slots=True)
class SessionStatusTransition:
    """Immutable presentation and routing data captured at a status transition."""

    session_id: str
    project_id: str
    agent_run_id: str | None
    status: str
    transitioned_at: datetime
    seq_num: int | None
    title: str | None
    source: str

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        status: str | None = None,
        transitioned_at: datetime | None = None,
    ) -> SessionStatusTransition:
        """Capture a transition without retaining the mutable session row."""
        return cls(
            session_id=session.id,
            project_id=session.project_id,
            agent_run_id=session.agent_run_id,
            status=status or session.status,
            transitioned_at=transitioned_at or session.updated_at,
            seq_num=session.seq_num,
            title=session.title,
            source=session.source,
        )


type SessionStatusTransitionCallback = Callable[[SessionStatusTransition], None]
