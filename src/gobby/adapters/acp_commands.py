"""ACP available command normalization."""

from __future__ import annotations

from typing import Any


def normalize_available_commands(commands: Any) -> list[dict[str, Any]]:
    """Return ACP available commands that match the current protocol schema."""
    if not isinstance(commands, list):
        return []

    normalized: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue

        name = command.get("name")
        description = command.get("description")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(description, str) or not description.strip():
            continue

        payload: dict[str, Any] = {
            "name": name.strip(),
            "description": description.strip(),
        }
        input_spec = command.get("input")
        if isinstance(input_spec, dict):
            hint = input_spec.get("hint")
            if isinstance(hint, str) and hint.strip():
                payload["input"] = {"hint": hint.strip()}
        normalized.append(payload)

    return normalized
