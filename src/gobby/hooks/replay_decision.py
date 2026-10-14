"""Hub-owned decisions shared by node replay and the local inbox drain."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gobby.hooks.grok_pending_context import decide_pending_inbox_envelope
from gobby.hooks.replay_fence import archive_superseded_hook
from gobby.hooks.runtime_compat import envelope_has_hook_response_capability
from gobby.storage.hook_receipts import HookReceipt
from gobby.storage.hub.protocol import HubDatabase


def acknowledge_and_apply_receipt(
    db: HubDatabase,
    *,
    receipt_id: str,
    delivery_generation: int,
    message_manager: Any,
    variable_manager: Any,
) -> HookReceipt | None:
    """Apply one-shot effects only for the winner of the receipt CAS."""
    from gobby.hooks.receipt_effects import apply_acknowledged_receipt
    from gobby.storage.hook_receipts import acknowledge_receipt

    committed = acknowledge_receipt(
        db, receipt_id=receipt_id, delivery_generation=delivery_generation
    )
    if committed is not None:
        apply_acknowledged_receipt(
            committed, message_manager=message_manager, variable_manager=variable_manager
        )
    return committed


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayDecision:
    disposition: Literal["retain", "requeued", "unlink", "archived", "below_floor", "execute"]
    detail: str = ""


def terminalize_below_floor_receipts(
    app: Any, envelope_id: str, *, machine_id: str | None = None
) -> None:
    """Best-effort retirement of delivery obligations from obsolete producers."""
    from gobby.storage.hook_receipts import terminalize_receipts_for_envelope

    state = getattr(app, "state", None)
    db = getattr(state, "database", None)
    if db is None:
        db = getattr(getattr(state, "hook_manager", None), "db", None)
    if db is not None:
        try:
            terminalize_receipts_for_envelope(db, envelope_id=envelope_id, machine_id=machine_id)
        except Exception:
            logger.warning(
                "Failed to terminalize below-floor receipts for %s", envelope_id, exc_info=True
            )


def decide_replay(
    app: Any,
    envelope_id: str | None,
    envelope: dict[str, Any],
    *,
    age_seconds: float,
    processed: bool,
    live: bool,
    machine_id: str | None = None,
) -> ReplayDecision:
    """Apply hub decisions in drain order; leave all filesystem effects to the caller."""
    capable = envelope_has_hook_response_capability(envelope.get("response_capability"))
    manager = getattr(getattr(app, "state", None), "hook_manager", None)
    if capable and envelope_id and manager is not None:
        try:
            pending = decide_pending_inbox_envelope(
                manager, envelope_id, envelope, age_seconds=age_seconds, machine_id=machine_id
            )
        except Exception:
            logger.exception("Cannot establish pending hook delivery for %s", envelope_id)
            return ReplayDecision("retain", "pending_delivery_lookup_failed")
        if pending is not None:
            return ReplayDecision(pending, "grok_pending_delivery")
    if processed:
        return ReplayDecision("unlink")
    if capable and live:
        return ReplayDecision("retain", "live")
    fence_detail = ""

    def archive(path: Path, *, reason: str, detail: str) -> bool:
        nonlocal fence_detail
        fence_detail = detail
        return True

    archived = archive_superseded_hook(
        app, envelope, Path(envelope_id or "hook"), archive, machine_id=machine_id
    )
    if archived is not None:
        return ReplayDecision("archived" if archived else "retain", fence_detail)
    if not capable:
        if envelope_id:
            terminalize_below_floor_receipts(app, envelope_id, machine_id=machine_id)
        return ReplayDecision(
            "below_floor", "request-carried response_capability is below hook-response.v1"
        )
    return ReplayDecision("execute")
