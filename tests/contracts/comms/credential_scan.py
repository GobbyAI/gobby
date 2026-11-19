"""Credential-shape scan over the communications and voice corpus and the provider edge scripts.

A committed case holds ``$secret:`` references only. The scan flags Slack bot tokens, Telegram bot
tokens and the generated fake-secret marker wherever they appear in a committed case or an edge
script. A finding names the file and the shape, never the matched value. The scan never reads the
secret store.

This module is a stub: the signatures are final, and every function returns an inert value until
the scan is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScanReport:
    """What one corpus scan covered and what it flagged."""

    case_files: int
    edge_files: int
    findings: list[str] = field(default_factory=list)


def scan_corpus() -> ScanReport:
    """Scan every v3 case file and every edge script."""
    return ScanReport(case_files=0, edge_files=0, findings=[])


def find_credential_shapes(text: str) -> list[str]:
    """The name of each credential shape found in ``text``, one name per match."""
    return []
