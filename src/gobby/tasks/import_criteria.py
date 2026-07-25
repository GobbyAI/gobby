"""Observable validation criteria for externally imported tasks."""

from __future__ import annotations


def external_issue_validation_criteria(source: str, reference: str) -> str:
    """Return an explicit completion contract for an imported issue."""
    return (
        f"The acceptance conditions recorded in {source} issue {reference} are implemented, "
        "and the resulting behavior is verified by authoritative current-state evidence."
    )
