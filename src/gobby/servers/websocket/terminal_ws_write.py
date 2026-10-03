"""Lease-gated operator writes: terminal input, paste and text."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from gobby.servers.websocket.terminal_input import WriteOutcome, record_turn_observation
from gobby.terminals.leases import TerminalLeaseRegistry, paste_oversize
from gobby.terminals.runtime import Delivered, IndeterminateWrite, TerminalWriteError

WRITE_FAULT_NAME = "terminal_write_fault"


def write_handler_faulted() -> bool:
    """True when the isolated-daemon write-handler fault file is present."""
    home = os.environ.get("GOBBY_HOME")
    if not home:
        return False
    return Path(home).joinpath(WRITE_FAULT_NAME).is_file()


class TerminalWriteMixin:
    """Admit operator writes through the lease registry and deliver them."""

    if TYPE_CHECKING:

        async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None: ...

        def _leases(self) -> TerminalLeaseRegistry: ...

    async def _handle_terminal_input(self, websocket: Any, data: dict[str, Any]) -> None:
        await self._handle_operator_write(websocket, data, kind="input")

    async def _handle_terminal_paste(self, websocket: Any, data: dict[str, Any]) -> None:
        text = data.get("text")
        if isinstance(text, str) and paste_oversize(text):
            await self._write_outcome(
                websocket,
                data,
                outcome="refused",
                reason="oversize",
            )
            return
        await self._handle_operator_write(websocket, data, kind="paste")

    async def _handle_operator_write(
        self,
        websocket: Any,
        data: dict[str, Any],
        *,
        kind: Literal["input", "paste", "text"],
    ) -> None:
        terminal_id = data.get("terminal_id")
        attachment_id = data.get("attachment_id")
        seq = data.get("client_write_seq")
        payload = data.get("data") if kind == "input" else data.get("text")
        if not isinstance(attachment_id, str) or not attachment_id:
            await self._write_outcome(
                websocket,
                data,
                outcome="refused",
                reason="attachment_required",
            )
            return
        if not isinstance(terminal_id, str):
            return
        if not isinstance(payload, str):
            payload = ""
        record = self._leases().get(attachment_id)
        generation = None if record is None else self._leases().generation(terminal_id)
        admitted = self._leases().admit_write(
            terminal_id,
            attachment_id=attachment_id,
            expected_lease_generation=generation
            if self._leases().holder(terminal_id) == attachment_id
            else -1,
            seq=seq,
            kind=kind,
            payload=payload.encode("utf-8"),
        )
        if not admitted.ok:
            await self._write_outcome(websocket, data, outcome="refused", reason=admitted.reason)
            return
        if admitted.recorded_outcome is not None:
            await self._write_outcome(
                websocket, data, outcome=admitted.recorded_outcome, reason=admitted.reason
            )
            return
        if admitted.join_inflight and isinstance(seq, int):
            joined = await self._wait_joined_write(attachment_id, seq)
            await self._write_outcome(websocket, data, outcome=joined[0], reason=joined[1])
            return
        if write_handler_faulted():
            if isinstance(seq, int):
                self._leases().complete_write(attachment_id, seq, "refused", "write_handler_fault")
            await self._write_outcome(
                websocket, data, outcome="refused", reason="write_handler_fault"
            )
            return
        outcome = "indeterminate"
        reason: str | None = "indeterminate_backend"
        try:
            outcome, reason = await self._deliver_operator_write(
                terminal_id,
                attachment_id,
                kind=kind,
                payload=payload,
                generation=generation,
                seq=seq,
            )
        finally:
            if isinstance(seq, int):
                self._leases().complete_write(attachment_id, seq, outcome, reason)
        try:
            await self._write_outcome(websocket, data, outcome=outcome, reason=reason)
        finally:
            # The observer looks the terminal row up for an interrupt key; the
            # client's reply must not wait on that.
            await record_turn_observation(
                self,
                terminal_id,
                kind=kind,
                payload=payload,
                outcome=outcome,
                seq=seq,
            )

    async def _deliver_operator_write(
        self,
        terminal_id: str,
        attachment_id: str,
        *,
        kind: Literal["input", "paste", "text"],
        payload: str,
        generation: int | None,
        seq: object = None,
    ) -> tuple[WriteOutcome, str | None]:
        """Deliver an admitted write to the backend; returns (outcome, reason)."""
        outcome: WriteOutcome = "delivered"
        reason: str | None = None
        coordinator = getattr(self, "write_coordinator", None)
        if coordinator is None:
            return "refused", "runtime_unavailable"
        try:
            from gobby.terminals.write_coordinator import (
                RuntimeUnavailableError,
                StaleTerminalLeaseError,
                WriteRequest,
            )

            record = self._leases().get(attachment_id)
            result = await coordinator.write(
                WriteRequest(
                    terminal_id=terminal_id,
                    action_key=f"ws:{attachment_id}:{seq}",
                    origin="operator",
                    kind=kind,
                    payload=payload,
                    attachment_id=attachment_id,
                    expected_lease_generation=generation,
                    terminal=None if record is None else record.terminal,
                )
            )
        except RuntimeUnavailableError:
            return "refused", "runtime_unavailable"
        except StaleTerminalLeaseError:
            return "refused", "lease_lost"
        except TerminalWriteError as exc:
            if exc.stage == "partial":
                reason = (
                    f"indeterminate_partial_delivered:{exc.delivered_bytes}"
                    if exc.delivered_bytes is not None
                    else "indeterminate_backend"
                )
                return "indeterminate", reason
            return "refused", "held"
        except (ConnectionError, OSError):
            return "indeterminate", "indeterminate_backend"
        if isinstance(result, IndeterminateWrite):
            outcome = "indeterminate"
            reason = "indeterminate_backend"
        elif not isinstance(result, Delivered):
            outcome = "refused"
            reason = "held"
        return outcome, reason

    async def _write_outcome(
        self,
        websocket: Any,
        data: dict[str, Any],
        *,
        outcome: str,
        reason: str | None,
    ) -> None:
        await self._send_json(
            websocket,
            {
                "type": "terminal_write_outcome",
                "terminal_id": data.get("terminal_id"),
                "attachment_id": data.get("attachment_id"),
                "client_write_seq": data.get("client_write_seq"),
                "outcome": outcome,
                "reason": reason,
            },
        )

    async def _wait_joined_write(self, attachment_id: str, seq: int) -> tuple[str, str | None]:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            completed = self._leases().completed_write(attachment_id, seq)
            if completed is not None:
                return completed
            await asyncio.sleep(0.01)
        return "indeterminate", "indeterminate_backend"
