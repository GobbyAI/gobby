"""Recognize authenticated operator communications for the wake focus hold."""

from __future__ import annotations

import json

from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.sessions import system_session_id


def is_operator_comms_wake(message: InterSessionMessage) -> bool:
    """Whether trusted daemon ingress sent this operator message.

    Such a wake waits on a focused holder of its terminal but never replaces a composer:
    a draft, held or unknown composer is withheld on every path (#25830 criterion 3).
    """
    if message.from_session != system_session_id():
        return False
    try:
        metadata = json.loads(message.metadata_json or "{}")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get("operator_comms") is True
        and metadata.get("wake_requested") is True
        and isinstance(metadata.get("communications_message_id"), str)
        and bool(metadata["communications_message_id"])
    )
