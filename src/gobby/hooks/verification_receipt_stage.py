"""First-class verification receipt stage for normalized hook events."""

from __future__ import annotations

import logging

from gobby.hooks.events import HookEvent, HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.verification_receipt_ingestion import (
    VerificationReceiptIngestionError,
    ingest_verification_receipt,
    is_verification_receipt_candidate,
    verification_receipt_identity,
)


def ingest_hook_verification_receipt(
    event: HookEvent,
    *,
    database: HubDatabase | None,
    logger: logging.Logger,
) -> None:
    """Persist a shell receipt after project and platform-session resolution."""
    if not is_verification_receipt_candidate(event):
        return

    identity = verification_receipt_identity(event)
    session_id = event.metadata.get("_platform_session_id")
    terminal = event.event_type == HookEventType.AFTER_TOOL
    if (
        not isinstance(session_id, str)
        or not session_id
        or not event.project_id
        or database is None
    ):
        logger.info(
            "Skipping verification receipt outside resolved project/session",
            extra={
                "event_type": event.event_type.value,
                "source": event.source.value,
                "identity": identity,
                "project_id": event.project_id,
                "session_id": session_id,
            },
        )
        return

    try:
        result = ingest_verification_receipt(
            event,
            session_id,
            db=database,
        )
    except Exception as exc:
        if terminal:
            logger.exception(
                "Verification receipt ingestion failed",
                extra={
                    "event_type": event.event_type.value,
                    "source": event.source.value,
                    "identity": identity,
                },
            )
            raise VerificationReceiptIngestionError(identity) from exc
        logger.warning(
            "Provisional verification receipt ingestion failed",
            extra={"source": event.source.value, "identity": identity},
            exc_info=True,
        )
        return

    if result is None:
        if terminal:
            raise VerificationReceiptIngestionError(identity)
        return
    logger.info(
        "Verification receipt acknowledged",
        extra={
            "source": event.source.value,
            "identity": result.receipt.execution_id,
            "outcome": result.normalized_outcome,
            "task_id": result.task_id,
            "projected": result.projection is not None,
            "replayed": result.replayed,
        },
    )
