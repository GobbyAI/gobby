"""Real handoff/wake proof helpers; canonical readback authorizes clear lineage."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from gobby.events.live_wake import RETRYABLE_WAKE_SKIPS
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    build_handoff_continue_prompt,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.e2e.composer_proof import ProofRefused, ProofScope, Surface
from tests.e2e.composer_proof_cleanup import cancel_caller, retire_terminal
from tests.e2e.composer_proof_trace import completion_envelope
from tests.e2e.conftest import AsyncMCPTestClient, daemon_token

if TYPE_CHECKING:
    from tests.e2e.composer_proof_live import LiveProof, Seat


def require_race_evidence(
    events: list[dict[str, object]],
    *,
    terminal_id: str,
    attempt_id: str,
    first: str,
    clear: bool,
    requested_session_id: str,
    failed: bool,
) -> None:
    events = [item for item in events if item.get("terminal_id") == terminal_id]
    locks = [
        item
        for item in events
        if item.get("phase") == "lock"
        and (item.get("writer") == "wake" or item.get("attempt_id") == attempt_id)
    ]
    acquisitions = [item for item in locks if item.get("state") == "acquired"]
    second = "handoff" if first == "wake" else "wake"
    queued = [
        item for item in locks if item.get("state") == "queued" and item.get("writer") == second
    ]
    if (
        first not in {"wake", "handoff"}
        or len(acquisitions) < 2
        or len(queued) != 1
        or [item.get("writer") for item in acquisitions[:2]] != [first, second]
        or len({item.get("lock_sha256") for item in [*acquisitions[:2], *queued]}) != 1
        or not (
            cast(int, acquisitions[0]["sequence"])
            < cast(int, queued[0]["sequence"])
            < cast(int, acquisitions[1]["sequence"])
        )
    ):
        raise ProofRefused("race has no exact original lock acquisition and waiter order")
    wakes = [item for item in events if item.get("phase") == "wake"]
    if (
        len(wakes) != 1
        or wakes[0].get("requested_session_id") != requested_session_id
        or wakes[0].get("delivered") is not False
        or wakes[0].get("skipped")
        not in (
            {
                "handoff_delivery_pending",
                "session_awaiting_handoff",
                "session_expired",
                "session_not_live",
                "session_active",
            }
            | (RETRYABLE_WAKE_SKIPS if failed else set())
        )
    ):
        raise ProofRefused("staged handoff wake did not preserve the real protected outcome")
    writes = [
        item
        for item in events
        if item.get("phase") in {"before", "after"} and item.get("origin") != "operator"
    ]
    command = "/clear" if clear else "/compact"
    command_hash = hashlib.sha256((command + "\n").encode()).hexdigest()
    if failed:
        if (
            any(item.get("outcome") == "Delivered" for item in writes)
            or any(item.get("payload_sha256") != command_hash for item in writes)
            or len([item for item in writes if item.get("phase") == "before"]) != 1
        ):
            raise ProofRefused("failed physical handoff wrote or retried unexpected bytes")
        return
    expected = [
        ("text", command_hash),
        ("key", hashlib.sha256(b"enter").hexdigest()),
        ("text", hashlib.sha256((build_handoff_continue_prompt() + "\n").encode()).hexdigest()),
        ("key", hashlib.sha256(b"enter").hexdigest()),
    ]
    before = [item for item in writes if item.get("phase") == "before"]
    after = [item for item in writes if item.get("phase") == "after"]
    if (
        [(item.get("kind"), item.get("payload_sha256")) for item in after] != expected
        or len(before) != 4
        or any(item.get("outcome") != "Delivered" for item in after)
        or any(
            (a.get("action_sha256"), a.get("kind"), a.get("payload_sha256"))
            != (b.get("action_sha256"), b.get("kind"), b.get("payload_sha256"))
            or cast(int, a["sequence"]) >= cast(int, b["sequence"])
            for a, b in zip(before, after, strict=True)
        )
        or (
            first == "handoff"
            and cast(int, acquisitions[1]["sequence"]) <= cast(int, after[1]["sequence"])
        )
    ):
        raise ProofRefused("race has duplicate, concatenated or unserialized automatic submission")


class ComposerRaces:
    def __init__(self, proof: LiveProof, variables: SessionVariableManager) -> None:
        self.proof, self.variables = proof, variables
        proof.trace.authorize_clear = self.authorize_clear

    async def race(
        self,
        seat: Seat,
        *,
        clear: bool,
        first: Literal["wake", "handoff"],
        cancel: bool = False,
        fail: bool = False,
    ) -> None:
        proof, trace = self.proof, self.proof.trace
        await proof.empty(seat)
        predecessor, external = seat.surface, seat.external_id
        start, hooks = len(trace.events), len(seat.ws.messages)
        revision = seat.frames.revision
        command = "/clear" if clear else "/compact"
        trace.arm_admission(predecessor, "wake")
        try:
            message = await proof.notice(seat, "urgent")
            await asyncio.wait_for(trace.admitted.wait(), 10)
            envelope = await self.stage(seat, clear=clear)
            attempt = envelope["input_data"]["tool_output"]["result"]["attempt_id"]
            trace.arm_before(predecessor, command)
            if first == "wake":
                trace.arm_wake_acquisition(predecessor)
                trace.admission_release.set()
                await asyncio.wait_for(trace.acquired.wait(), 10)
            await self.relay(seat, envelope)
            if first == "wake":
                await trace.wait_for(
                    lambda item: item.get("phase") == "lock"
                    and item.get("state") == "queued"
                    and item.get("writer") == "handoff"
                    and item.get("attempt_id") == attempt,
                    after=start,
                    timeout=10,
                )
                trace.acquisition_release.set()
            held = await proof.held_boundary(
                seat, command, after=revision, hooks_after=hooks, before_write=True
            )
            if first == "handoff":
                trace.admission_release.set()
                await trace.wait_for(
                    lambda item: item.get("phase") == "lock"
                    and item.get("state") == "queued"
                    and item.get("writer") == "wake",
                    after=start,
                    timeout=10,
                )
            fresh = await proof.held_boundary(
                seat, command, after=seat.frames.revision, hooks_after=hooks, before_write=True
            )
            if fresh.cursor != held.cursor:
                raise ProofRefused("held composer cursor changed while actual waiter queued")
            if cancel:
                await cancel_caller(trace.socket.parent / "cleanup.sock", predecessor, attempt)
            if fail:
                await proof.terminate(seat)
            trace.release.set()
            await trace.wait_for(
                lambda item: item.get("phase") == "wake"
                and item.get("requested_session_id") == predecessor.session_id,
                after=start,
                timeout=120,
            )
            if fail:
                await retire_terminal(trace.socket.parent / "cleanup.sock", predecessor)
            if not fail:
                await self.boundary(seat, predecessor, external, attempt, clear, hooks, revision)
            await self.settle(
                seat,
                predecessor=predecessor,
                attempt_id=attempt,
                clear=clear,
                after=start,
                outcome="failed" if fail else "delivered",
                caller_state="cancelled" if cancel else "settled",
            )
            require_race_evidence(
                trace.events[start:],
                terminal_id=predecessor.terminal_id,
                attempt_id=attempt,
                first=first,
                clear=clear,
                requested_session_id=predecessor.session_id,
                failed=fail,
            )
            if fail and any(
                item.get("type") == "hook_event"
                and item.get("session_id") == external
                and item.get("event_type")
                in {"user-prompt-submit", "post-compact", "session-start"}
                for item in seat.ws.messages[hooks:]
            ):
                raise ProofRefused("failed physical handoff produced a provider boundary")
            await proof.durable(predecessor, message)
            proof.evidence.append(
                {
                    "case": "handoff_race",
                    "provider": predecessor.provider,
                    "predecessor_session_id": predecessor.session_id,
                    "continuation_session_id": seat.surface.session_id,
                    "terminal_id": predecessor.terminal_id,
                    "attempt_id": attempt,
                    "message_id": message,
                    "first": first,
                    "clear": clear,
                    "cancel": cancel,
                    "failed": fail,
                    "barrier": "before original text dispatch; original composer lock held",
                }
            )
        finally:
            trace.release.set()
            trace.admission_release.set()
            trace.acquisition_release.set()

    async def boundary(
        self,
        seat: Seat,
        predecessor: Surface,
        external: str,
        attempt: str,
        clear: bool,
        hooks: int,
        revision: int,
    ) -> None:
        def is_boundary(item: dict[str, Any]) -> bool:
            if item.get("type") != "hook_event" or item not in seat.ws.messages[hooks:]:
                return False
            if clear:
                return (
                    item.get("event_type") == "session-start" and item.get("session_id") != external
                )
            return item.get("session_id") == external and (
                item.get("event_type") == "post-compact"
                or (
                    item.get("event_type") == "session-start"
                    and item.get("data", {}).get("source") == "compact"
                )
            )

        boundary = await seat.ws.wait_for(
            is_boundary, timeout=650, description="real provider handoff boundary"
        )
        if clear:
            row = await self.proof.terminal(predecessor.terminal_id)
            successor = Surface(
                predecessor.provider,
                str(row.get("session_id")),
                predecessor.terminal_id,
                self.proof.locator(row).frame_host_epoch,
                str(row.get("project_id")),
            )
            if successor == predecessor:
                raise ProofRefused("real clear boundary did not bind a successor")
            # This is canonical row readback, not a fabricated lifecycle or trace receipt.
            await self.proof.trace.require_event_surface(
                {**successor.__dict__, "attempt_id": attempt}, False
            )
            if boundary.get("session_id") != seat.external_id:
                raise ProofRefused("provider clear boundary belongs to another physical seat")
        continuation = build_handoff_continue_prompt()
        await seat.ws.wait_for(
            lambda item: item.get("type") == "hook_event"
            and item.get("event_type") == "user-prompt-submit"
            and item.get("session_id") == seat.external_id
            and item.get("data", {}).get("prompt_text", item.get("data", {}).get("prompt"))
            == continuation
            and item in seat.ws.messages[hooks:],
            timeout=120,
            description="real provider continuation submission",
        )
        await self.proof.empty(seat, timeout=180, after=revision)
        owned = [
            item
            for item in seat.ws.messages[hooks:]
            if item.get("type") == "hook_event"
            and item.get("session_id") in {external, seat.external_id}
        ]
        prompts = [
            item.get("data", {}).get("prompt_text", item.get("data", {}).get("prompt"))
            for item in owned
            if item.get("event_type") == "user-prompt-submit"
        ]
        if (
            prompts.count(continuation) != 1
            or any(
                prompt not in {continuation, "/clear" if clear else "/compact"}
                for prompt in prompts
            )
            or len(prompts) > 2
            or any(
                sum(item.get("event_type") == kind for item in owned) > 1
                for kind in ("post-compact", "session-start")
            )
        ):
            raise ProofRefused("extra or concatenated provider submission/boundary")
        self.proof.evidence.append(
            {
                "case": "provider_boundary",
                "attempt_id": attempt,
                "event_type": boundary["event_type"],
                "timestamp": boundary.get("timestamp"),
                "continuation_submissions": 1,
                "command_submission_hooks": len(prompts) - 1,
            }
        )

    async def relay(self, seat: Seat, envelope: dict[str, Any]) -> None:
        """Submit the controller's actual MCP completion, without provider lifecycle events."""
        await self.proof.validate(seat)
        own = seat.surface
        self.proof.trace.scope.require_surface(own, own)
        details = await self.proof.call(
            "gobby-sessions", "get_session", {"session_id": own.session_id}
        )
        if (
            seat not in self.proof.seats
            or own.session_id not in self.proof.handoffs_pending
            or details.get("id") != own.session_id
            or details.get("source") != own.provider
            or details.get("project_id") != own.project_id
            or details.get("external_id") != seat.external_id
        ):
            raise ProofRefused("completion provider identity changed")
        data = envelope.get("input_data")
        if not isinstance(data, dict) or not isinstance(data.get("tool_output"), dict):
            raise ProofRefused("completion has no real staged result")
        expected = completion_envelope(
            own, seat.external_id, str(details.get("machine_id")), data["tool_output"]
        )
        expected["enqueued_at"] = envelope.get("enqueued_at")
        if expected != envelope:
            raise ProofRefused("completion transport identity or result changed")
        response = await self.proof.http.post("/api/hooks/execute", json=envelope, timeout=60)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("continue") is False or result.get("error"):
            raise ProofRefused("public completion hook refused")
        self.proof.evidence.append(
            {
                "case": "handoff_completion",
                "provider": own.provider,
                "session_id": own.session_id,
                "attempt_id": data["tool_output"]["result"]["attempt_id"],
                "transport": "controller public hook; real staged result",
            }
        )

    async def pull(self, session_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = AsyncMCPTestClient(
            self.proof.daemon.http_url, daemon_token(self.proof.daemon.gobby_home)
        )
        client.session_id = session_id
        try:
            schema = await client.client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "gobby-sessions", "tool_name": "get_handoff"},
                headers=client._session_headers(),
            )
            schema.raise_for_status()
            if schema.json().get("success") is not True:
                raise ProofRefused("public handoff pull schema refused")
            raw = await client.call_tool("gobby-sessions", "get_handoff", arguments)
            result = raw.get("result")
            if (
                raw.get("success") is not True
                or not isinstance(result, dict)
                or result.get("success") is not True
                or "error" in result
            ):
                raise ProofRefused("public handoff pull refused")
            return result
        finally:
            await client.close()

    async def settle(
        self,
        seat: Seat,
        *,
        predecessor: Surface,
        attempt_id: str,
        clear: bool,
        after: int,
        outcome: Literal["delivered", "failed"],
        caller_state: Literal["settled", "cancelled"] = "settled",
    ) -> None:
        """Verify original caller settlement and canonical storage before allowing cleanup."""
        own = seat.surface
        self.proof.scope.require_surface(predecessor, predecessor)
        self.proof.scope.require_surface(own, self.proof.trace.surfaces[own.terminal_id])
        if (
            seat not in self.proof.seats
            or predecessor.session_id not in self.proof.handoffs_pending
            or (
                own != predecessor
                and (
                    not clear
                    or self.proof.trace.predecessors.get((predecessor.session_id, attempt_id))
                    != predecessor
                )
            )
        ):
            raise ProofRefused("settlement has no exact owned attempt binding")
        await self.proof.trace.wait_for(
            lambda item: item.get("phase") == "caller"
            and item.get("attempt_id") == attempt_id
            and item.get("session_id") in {predecessor.session_id, own.session_id}
            and item.get("state") == caller_state,
            after=after,
            timeout=700,
        )
        snapshot = await asyncio.to_thread(self.variables.get_variables, predecessor.session_id)
        failed = outcome == "failed"
        marker = snapshot.get(FAILED_HANDOFF_VARIABLE if failed else PENDING_HANDOFF_VARIABLE)
        if (
            not isinstance(marker, Mapping)
            or marker.get("attempt_id") != attempt_id
            or (not failed and marker.get("clear_session") is not clear)
            or not isinstance(marker.get("handoff_record_id"), str)
        ):
            raise ProofRefused("settled attempt has no canonical authored marker")
        if failed:
            gate = snapshot.get(HANDOFF_DISPATCH_GATE_VARIABLE)
            if (
                PENDING_HANDOFF_VARIABLE in snapshot
                or marker.get("delivery_state") != "failed_not_deliverable"
                or not isinstance(gate, Mapping)
                or gate.get("attempt_id") != attempt_id
                or gate.get("clear_session") is not clear
                or gate.get("delivery_pending") is not False
                or not (
                    gate.get("delivery_failed") is True or gate.get("delivery_abandoned") is True
                )
            ):
                raise ProofRefused("failed attempt is not durably settled")
        record_id = marker["handoff_record_id"]
        record = await asyncio.to_thread(
            self.variables.db.fetchone,
            "SELECT id, session_id, rendered_markdown FROM session_handoffs "
            "WHERE id = %s AND session_id = %s",
            (record_id, predecessor.session_id),
        )
        if (
            record is None
            or str(record["id"]) != record_id
            or str(record["session_id"]) != predecessor.session_id
            or not isinstance(record["rendered_markdown"], str)
            or not record["rendered_markdown"].strip()
        ):
            raise ProofRefused("canonical authored handoff missing")
        expected = (
            []
            if failed
            else [
                {
                    "handoff_id": record_id,
                    "attempt_id": attempt_id,
                    "boundary_kind": "clear" if clear else "compact",
                    "continuation_session_id": own.session_id,
                }
            ]
        )

        async def require_receipts() -> None:
            rows = await asyncio.to_thread(
                self.variables.db.fetchall,
                "SELECT handoff_id, attempt_id, boundary_kind, continuation_session_id "
                "FROM session_handoff_deliveries WHERE attempt_id = %s OR handoff_id = %s",
                (attempt_id, record_id),
            )
            observed = [{key: str(value) for key, value in row.items()} for row in rows]
            if observed != expected:
                raise ProofRefused("canonical handoff receipt count or identity differs")

        await require_receipts()
        for index in range(2):
            result = await self.pull(
                own.session_id, {"failed_attempt_id": attempt_id} if failed else {}
            )
            if failed or index == 0:
                if (
                    result.get("found") is not True
                    or result.get("session_id") != predecessor.session_id
                    or result.get("handoff") != record["rendered_markdown"]
                    or (
                        failed
                        and (
                            result.get("attempt_id") != attempt_id
                            or result.get("delivery_state") != "failed_not_deliverable"
                        )
                    )
                ):
                    raise ProofRefused("public handoff pull differs from the canonical attempt")
            elif result.get("found") is not False or result.get("handoff") != "":
                raise ProofRefused("delivered handoff was consumed more than once")
        await require_receipts()
        if failed and snapshot != await asyncio.to_thread(
            self.variables.get_variables, predecessor.session_id
        ):
            raise ProofRefused("failed recovery mutated canonical attempt state")
        self.proof.evidence.append(
            {
                "case": "handoff_settlement",
                "attempt_id": attempt_id,
                "predecessor_session_id": predecessor.session_id,
                "continuation_session_id": own.session_id,
                "clear_session": clear,
                "caller_state": caller_state,
                "outcome": outcome,
                "receipt_count": len(expected),
                "handoff_sha256": hashlib.sha256(record["rendered_markdown"].encode()).hexdigest(),
                "transport": "controller bound public get_handoff; canonical readback",
            }
        )
        self.proof.handoffs_pending.remove(predecessor.session_id)

    async def stage(self, seat: Seat, *, clear: bool) -> dict[str, Any]:
        await self.proof.validate(seat)
        own = seat.surface
        self.proof.trace.scope.require_surface(own, own)
        details = await self.proof.call(
            "gobby-sessions", "get_session", {"session_id": own.session_id}
        )
        if (
            details.get("id") != own.session_id
            or details.get("source") != own.provider
            or details.get("project_id") != own.project_id
            or details.get("external_id") != seat.external_id
        ):
            raise ProofRefused("staging provider identity changed")
        client = AsyncMCPTestClient(
            self.proof.daemon.http_url, daemon_token(self.proof.daemon.gobby_home)
        )
        client.session_id = own.session_id
        leases: set[tuple[str, str]] = set()

        async def call(server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if (server, tool) not in leases:
                schema = await client.client.post(
                    "/api/mcp/tools/schema",
                    json={"server_name": server, "tool_name": tool},
                    headers=client._session_headers(),
                )
                schema.raise_for_status()
                if schema.json().get("success") is not True:
                    raise ProofRefused("public bound schema refused")
                leases.add((server, tool))
            raw = await client.call_tool(server, tool, arguments)
            result = raw.get("result")
            if (
                raw.get("success") is not True
                or not isinstance(result, dict)
                or result.get("success") is False
                or "error" in result
            ):
                raise ProofRefused(f"public bound {server}:{tool} refused")
            return raw

        try:
            arguments: dict[str, Any] = {"name": "gobby", "path": "references/sessions/handoffs.md"}
            seen: set[str] = set()
            while True:
                raw = await call("gobby-skills", "get_skill_file", arguments)
                entry, page = raw["result"].get("file"), raw["result"].get("page")
                if (
                    not isinstance(entry, dict)
                    or entry.get("skill_name") != "gobby"
                    or entry.get("path") != "references/sessions/handoffs.md"
                    or not isinstance(entry.get("content"), str)
                    or not entry["content"].strip()
                    or re.search(r"…\d+ tokens truncated…", entry["content"])
                    or not isinstance(page, dict)
                ):
                    raise ProofRefused("handoff instructions were not delivered completely")
                cursor = page.get("next_cursor")
                if cursor is None:
                    if page.get("complete") is not True:
                        raise ProofRefused("handoff instructions are incomplete")
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ProofRefused("handoff instruction cursor is invalid")
                seen.add(cursor)
                arguments = {"cursor": cursor}
            await call("gobby-sessions", "feedback", {"observations": []})
            self.proof.handoffs_pending.add(own.session_id)
            raw = await call(
                "gobby-sessions",
                "set_handoff",
                {
                    "current_state": "Owned isolated composer race proof at a real provider boundary.",
                    "next_steps": ["Consume this exact handoff after the real provider boundary."],
                    "clear_session": clear,
                },
            )
            envelope = completion_envelope(
                own, seat.external_id, str(details.get("machine_id")), raw
            )
            if raw["result"]["clear_session"] is not clear:
                raise ProofRefused("staged boundary differs from the requested boundary")
            if clear:
                self.proof.trace.expect_clear(own, raw["result"]["attempt_id"])
            self.proof.evidence.append(
                {
                    "case": "handoff_stage",
                    "provider": own.provider,
                    "session_id": own.session_id,
                    "attempt_id": raw["result"]["attempt_id"],
                    "clear_session": clear,
                    "transport": "controller public MCP; real staged result",
                }
            )
            return envelope
        finally:
            await client.close()

    async def authorize_clear(
        self, predecessor: Surface, successor: Surface, attempt_id: str
    ) -> None:
        seats = [seat for seat in self.proof.seats if seat.surface == predecessor]
        if len(seats) != 1:
            raise ProofRefused("clear predecessor is not one owned proof seat")
        seat = seats[0]
        row = await self.proof.terminal(predecessor.terminal_id)
        locator = self.proof.locator(row)
        observed = Surface(
            predecessor.provider,
            str(row.get("session_id")),
            str(row.get("id")),
            locator.frame_host_epoch,
            str(row.get("project_id")),
        )
        self.proof.scope.require_surface(successor, observed)
        if locator != seat.locator:
            raise ProofRefused("clear successor native attachment binding changed")
        before = await self.proof.call(
            "gobby-sessions", "get_session", {"session_id": predecessor.session_id}
        )
        after = await self.proof.call(
            "gobby-sessions", "get_session", {"session_id": successor.session_id}
        )
        variables = await asyncio.to_thread(self.variables.get_variables, predecessor.session_id)
        marker = variables.get(CLEAR_ATTEMPT_VARIABLE)
        if not isinstance(marker, Mapping):
            raise ProofRefused("clear successor has no canonical marker")
        require_clear_successor(
            self.proof.scope, predecessor, successor, attempt_id, marker, before, after
        )
        seat.surface, seat.external_id = successor, str(after["external_id"])
        self.proof.evidence.append(
            {
                "case": "clear_successor",
                "attempt_id": attempt_id,
                "predecessor_session_id": predecessor.session_id,
                "successor_session_id": successor.session_id,
                "terminal_id": successor.terminal_id,
                "host_epoch": successor.host_epoch,
                "readback": "public sessions and terminal; canonical consumed clear marker",
            }
        )


def require_clear_successor(
    scope: ProofScope,
    predecessor: Surface,
    successor: Surface,
    attempt_id: str,
    marker: Mapping[str, Any],
    predecessor_details: Mapping[str, Any],
    successor_details: Mapping[str, Any],
) -> None:
    """Require actual marker consumption and parentage before adopting a new identity."""
    scope.require_surface(predecessor, predecessor)
    scope.require_surface(successor, successor)
    if (
        predecessor.session_id == successor.session_id
        or (
            predecessor.provider,
            predecessor.terminal_id,
            predecessor.host_epoch,
            predecessor.project_id,
        )
        != (successor.provider, successor.terminal_id, successor.host_epoch, successor.project_id)
        or marker.get("attempt_id") != attempt_id
        or marker.get("consumed_by") != successor.session_id
        or not marker.get("handoff_record_id")
        or successor_details.get("parent_session_id") != predecessor.session_id
        or predecessor_details.get("id") != predecessor.session_id
        or successor_details.get("id") != successor.session_id
        or predecessor_details.get("source") != predecessor.provider
        or successor_details.get("source") != successor.provider
        or predecessor_details.get("project_id") != predecessor.project_id
        or successor_details.get("project_id") != successor.project_id
        or predecessor_details.get("machine_id") != successor_details.get("machine_id")
        or predecessor_details.get("external_id") == successor_details.get("external_id")
    ):
        raise ProofRefused("clear successor lacks canonical lineage or physical binding")
    try:
        for value in (
            attempt_id,
            marker["handoff_record_id"],
            predecessor_details["machine_id"],
            predecessor_details["external_id"],
            successor_details["external_id"],
        ):
            UUID(str(value))
    except (KeyError, ValueError, TypeError) as exc:
        raise ProofRefused("clear successor identity is invalid") from exc
