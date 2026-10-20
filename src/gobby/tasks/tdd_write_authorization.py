"""Authorize production writes only after every named behavior test has its own RED."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from gobby.storage.session_models import Session
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.transcript_evidence_models import TranscriptEvidence


@dataclass
class TddObligation:
    """One named test a TDD task's behavior or regression criterion requires."""

    task_id: str
    criterion: str
    test: AcceptanceTest
    regression_only: bool = False


@dataclass
class WriteAuthorization:
    """Whether a production write may proceed, with missing and credited REDs."""

    allowed: bool
    missing: tuple[str, ...]
    red_runs: Mapping[str, str]


def task_tdd_obligations(task_id: str, validation_criteria: str | None) -> tuple[TddObligation, ...]:
    raise NotImplementedError


def authorize_production_write(
    obligations: tuple[TddObligation, ...],
    evidence: TranscriptEvidence,
    *,
    task_refs: frozenset[str],
) -> WriteAuthorization:
    raise NotImplementedError


def authorize_session_write(
    session: Session,
    obligations: tuple[TddObligation, ...],
    *,
    task_refs: frozenset[str],
    repo_path: str,
    archive_dir: str | None = None,
) -> WriteAuthorization:
    raise NotImplementedError
