"""Shared communications responder command definitions."""

from __future__ import annotations

RESPONDER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("new", "Start a new conversation"),
    ("reset", "Reset the current conversation"),
    ("stop", "Stop the active response"),
    ("status", "Show responder provider and model"),
    ("subscriptions", "Manage event subscriptions"),
    ("help", "Show available commands"),
)


def telegram_bot_commands() -> list[dict[str, str]]:
    """Return a fresh Telegram BotCommand payload for the responder commands."""
    return [
        {"command": command, "description": description}
        for command, description in RESPONDER_COMMANDS
    ]


def responder_help_text() -> str:
    """Render the responder commands for the transport-neutral help response."""
    commands = ", ".join(f"/{command}" for command, _description in RESPONDER_COMMANDS)
    return f"Commands: {commands}"
