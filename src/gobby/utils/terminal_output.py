"""Redaction for captured agent diagnostics and persisted output."""

from __future__ import annotations

import re
from pathlib import Path

# Order matters: specific secrets first, then the home-directory path.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-...
    (re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"), "sk-<redacted>"),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_, github_pat_
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{22,})"),
        "gh<redacted-token>",
    ),
    # AWS access key IDs
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA<redacted>"),
    # Bearer / Authorization tokens
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
        r"\1 <redacted>",
    ),
    # Generic secret-bearing key/value assignments
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)"
            r"(\s*[:=]\s*[\"']?)([^\s\"']{12,})"
        ),
        r"\1\2<redacted>",
    ),
)


def redact_terminal_output(text: str) -> str:
    """Scrub secrets and the operator home path from captured terminal output."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    home = str(Path.home())
    if home and home != "/" and home in redacted:
        redacted = redacted.replace(home, "~")
    return redacted
