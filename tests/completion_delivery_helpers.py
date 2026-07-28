from __future__ import annotations

from typing import Any

import pytest


class DeliveryRegistry:
    """Recording registry whose notify returns a configurable delivered map."""

    def __init__(self, delivery: dict[str, bool] | None) -> None:
        self._delivery = delivery
        self.notify_calls: list[tuple[str, dict[str, Any] | None, str]] = []
        self.cleanup_calls: list[str] = []

    async def notify(
        self,
        run_id: str,
        *,
        result: dict[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, bool] | None:
        self.notify_calls.append((run_id, result, message))
        return self._delivery

    def cleanup(self, run_id: str) -> None:
        self.cleanup_calls.append(run_id)


def record_removals(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, list[str] | None]]:
    import gobby.agents.completion_subscribers as subscribers_module

    removals: list[tuple[str, list[str] | None]] = []

    def record(*, db: object, run_id: str, session_ids: list[str] | None = None) -> None:
        removals.append((run_id, session_ids))

    monkeypatch.setattr(subscribers_module, "remove_agent_completion_subscribers", record)
    return removals
