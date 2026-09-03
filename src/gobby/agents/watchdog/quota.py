"""Recognize provider usage-limit exhaustion in visible agent output."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CODEX_USAGE_LIMIT_MESSAGE = "You've hit your usage limit"
_CODEX_RESET_PREFIX = "try again at "
_PANE_SCAN_LINE_LIMIT = 15
_PANE_DECORATION = " │┃┆┇┊┋"


@dataclass(frozen=True)
class ProviderQuotaExhaustion:
    """A conclusive provider quota message parsed from a pane tail."""

    provider: str
    reset_time: str | None = None

    @property
    def error(self) -> str:
        reset = f"; reset_time={self.reset_time}" if self.reset_time is not None else ""
        return f"Provider quota exhausted: provider={self.provider}{reset}"


def _visible_line(raw_line: str) -> str:
    without_ansi = _ANSI_ESCAPE_RE.sub("", raw_line)
    without_controls = _TERMINAL_CONTROL_RE.sub("", without_ansi)
    return without_controls.strip().lstrip(_PANE_DECORATION)


def detect_provider_quota(pane_output: str, provider_id: str) -> ProviderQuotaExhaustion | None:
    """Return conclusive quota details for exact provider-owned pane messages."""
    provider = provider_id.strip().lower()
    if provider != "codex":
        return None

    lines = [_visible_line(line) for line in pane_output.splitlines()[-_PANE_SCAN_LINE_LIMIT:]]
    for index, line in enumerate(lines):
        if not line.startswith(_CODEX_USAGE_LIMIT_MESSAGE):
            continue
        message = " ".join(part for part in lines[index:] if part)
        reset_time: str | None = None
        reset_start = message.find(_CODEX_RESET_PREFIX)
        if reset_start >= 0:
            reset_value = message[reset_start + len(_CODEX_RESET_PREFIX) :]
            reset_time = reset_value.partition(".")[0].strip() or None
        return ProviderQuotaExhaustion(provider=provider, reset_time=reset_time)
    return None
