"""Host-listing lookups the native runtime inherits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.storage.terminals import AttachLocator
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.runtime import PreparedSpawn


class NativeHostProbeMixin:
    _client: Any

    if TYPE_CHECKING:

        async def _bind_frames(self, locator: AttachLocator, reservation_id: str) -> None: ...

    @property
    def host_epoch(self) -> str:
        """The connected host's epoch; empty before the client has connected."""
        return str(getattr(self._client, "host_epoch", "") or "")

    async def find_host_terminal(self, terminal_id: str, spawn_key: str) -> str | None:
        """Strict probe: the host id listed for ``(terminal_id, spawn_key)``, or None.

        Raises when the host cannot answer; unlike ``is_live`` it never reads a
        failed listing as absence.
        """
        rows: list[HostListRow] = await self._client.list_terminals()
        for row in rows:
            if str(row.terminal_id) == terminal_id and str(row.spawn_key) == spawn_key:
                return str(row.host_terminal_id)
        return None

    async def rebind_prepared(
        self,
        prepared: PreparedSpawn,
        reservation_id: str | None = None,
    ) -> None:
        rows: list[HostListRow] = await self._client.list_terminals()
        match = next(
            (
                row
                for row in rows
                if str(row.terminal_id) == str(prepared.terminal_id)
                and str(row.spawn_key) == prepared.spawn_key
            ),
            None,
        )
        if match is None:
            raise HostCommandError("not_found")
        if match.observer_bind == "none":
            raise HostCommandError("observer_bind_none")
        rid = reservation_id
        attaches = getattr(self._client, "attaches", None)
        if isinstance(attaches, list):
            attaches.append(rid)
        if rid is not None:
            locator = AttachLocator(
                backend="native",
                frame_host_epoch=str(getattr(self._client, "host_epoch", "") or ""),
                host_terminal_id=match.host_terminal_id,
            )
            await self._bind_frames(locator, rid)
