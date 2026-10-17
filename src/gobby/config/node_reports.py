"""Hub byte budgets for node run reports."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NodeReportsConfig(BaseModel):
    """Byte budgets the hub applies to node run reports."""

    model_config = ConfigDict(extra="forbid")

    max_request_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        description=(
            "Largest node request body the hub reads, for run reports and checkout "
            "registrations; larger bodies get 413."
        ),
    )
    max_pending_bytes_per_machine: int = Field(
        default=16 * 1024 * 1024,
        ge=1,
        description=(
            "Report data the hub holds for one machine until it is taken; a report that "
            "would exceed it gets 429 and is retried."
        ),
    )
