"""Cheap text triage for whether a user prompt is worth a memory search.

Recovered from the retired recall runner's hard-skip ladder (#21009). Only the
text heuristics live here; the session-state half of that ladder -- spawned
agents, the parent turn counter -- belongs in the rule's ``when``, where the
engine can read it without a tool call.
"""

from __future__ import annotations

import re

_SHORT_ACKNOWLEDGMENTS = frozenset(
    {
        "ack",
        "approved",
        "awesome",
        "cool",
        "correct",
        "exactly",
        "fine",
        "good",
        "got it",
        "great",
        "k",
        "lgtm",
        "makes sense",
        "nice",
        "ok",
        "okay",
        "perfect",
        "right",
        "sounds good",
        "sure",
        "thanks",
        "thank you",
        "understood",
        "yes",
        "yep",
    }
)
_CONTINUATIONS = frozenset(
    {
        "carry on",
        "continue",
        "go ahead",
        "keep going",
        "please continue",
        "proceed",
        "resume",
    }
)
_WAITS = frozenset({"hold", "hold on", "pause", "stop", "wait", "wait a moment"})
_STATUS_PATTERN = re.compile(
    r"^(?:any\s+)?(?:status(?:\s+update)?|progress|updates?)(?:\s+please)?\??$|"
    r"^(?:what(?:'s| is)|how(?:'s| is))\s+(?:the\s+)?(?:status|progress|it going)\??$|"
    r"^(?:are\s+you|is\s+it)\s+done\??$|^(?:done\s+yet|where\s+are\s+we|"
    r"how\s+far\s+along)\??$",
    re.IGNORECASE,
)
_SKILL_COMMAND_PATTERN = re.compile(
    r"^(?:please\s+)?(?:load|reload|use|install|uninstall|enable|disable)\b.*\bskill\b",
    re.IGNORECASE,
)
_LIFECYCLE_COMMAND_PATTERN = re.compile(
    r"^(?:please\s+)?(?:compact|handoff|resume|start|stop|end|close|pause)\b"
    r".*\b(?:agent|goal|session|task|turn)\b",
    re.IGNORECASE,
)
# A lifecycle verb only names an instruction this short; past it the prompt is
# describing work, whatever verb it opened with.
_MAX_LIFECYCLE_COMMAND_WORDS = 12


def is_substantive_prompt(text: str) -> bool:
    """Return whether *text* states work a memory search could inform."""
    normalized = " ".join(text.casefold().split()).strip(" .!?,")
    if not normalized:
        return False
    if normalized in _SHORT_ACKNOWLEDGMENTS:
        return False
    if normalized in _CONTINUATIONS:
        return False
    if normalized in _WAITS:
        return False
    if _STATUS_PATTERN.fullmatch(normalized):
        return False
    if normalized.startswith("/") or _SKILL_COMMAND_PATTERN.match(normalized):
        return False
    return not (
        len(normalized.split()) <= _MAX_LIFECYCLE_COMMAND_WORDS
        and _LIFECYCLE_COMMAND_PATTERN.match(normalized)
    )
