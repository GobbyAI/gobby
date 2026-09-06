"""Narrow helpers for terminal input delivery side effects."""

from __future__ import annotations

from typing import Any, Literal

WriteKind = Literal["input", "paste", "text"]
WriteOutcome = Literal["delivered", "indeterminate", "refused"]


def record_turn_observation(
    owner: Any,
    terminal_id: str,
    *,
    kind: WriteKind,
    payload: str,
    outcome: WriteOutcome,
    seq: object,
) -> None:
    """Report an admitted operator write without affecting delivery semantics."""
    observer = getattr(owner, "terminal_turn_observer", None)
    if observer is None:
        return
    input_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
    observer.record_mediated_input(
        terminal_id,
        payload if kind == "input" else "",
        outcome,
        input_seq=input_seq,
    )
