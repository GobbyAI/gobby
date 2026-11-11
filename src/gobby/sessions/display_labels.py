"""Display labels derived from the agent definition name.

Sessions carry no stored title. A seat is named by its definition, so every
surface that labels a defined seat calls this one helper.
"""

_PROVIDER_DISPLAY_NAMES = {
    "agy": "Antigravity",
    "claude": "Claude",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "droid": "Droid",
    "grok": "Grok",
    "pipeline": "Pipeline",
    "system": "System",
    "unknown": "Unknown",
}


def definition_display_name(agent_name: str) -> str:
    """Return the label for a definition name, such as ``build-helper`` -> ``Build Helper``."""
    return agent_name.replace("-", " ").replace("_", " ").title()


def provider_display_name(source: str) -> str:
    """Return the label for a session's provider when its seat has no definition."""
    normalized = source.strip()
    if not normalized:
        return "Unknown"
    return _PROVIDER_DISPLAY_NAMES.get(normalized.lower(), normalized)
