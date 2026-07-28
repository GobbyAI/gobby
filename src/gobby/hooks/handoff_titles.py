"""Built-in title extraction for canonical Codex plan handoffs."""

from __future__ import annotations

import re

from gobby.memory.title_heuristics import normalize_title_candidate

CODEX_PLAN_HANDOFF_PREFIX = (
    "A previous agent produced the plan below to accomplish the user's task. "
    "Implement the plan in a fresh context. Treat the plan as the source of user intent, "
    "re-read files as needed, and carry the work through implementation and verification."
)

_MARKDOWN_H1_RE = re.compile(
    r"^ {0,3}#[ \t]+(?P<title>.*?\S)(?:[ \t]+#+)?[ \t]*$",
)

__all__ = ["CODEX_PLAN_HANDOFF_PREFIX", "extract_codex_handoff_title"]


def extract_codex_handoff_title(prompt: object) -> str | None:
    """Extract a validated H1 from the canonical Codex plan-handoff prompt."""
    if not isinstance(prompt, str):
        return None

    stripped_prompt = prompt.lstrip()
    if not stripped_prompt.startswith(CODEX_PLAN_HANDOFF_PREFIX):
        return None

    remainder = stripped_prompt[len(CODEX_PLAN_HANDOFF_PREFIX) :]
    if remainder and remainder[0] not in "\r\n":
        return None

    first_content_line = next(
        (line for line in remainder.splitlines() if line.strip()),
        None,
    )
    if first_content_line is None:
        return None

    match = _MARKDOWN_H1_RE.fullmatch(first_content_line)
    if match is None:
        return None
    return normalize_title_candidate(match.group("title"))
