"""Credential-shape scan over the communications and voice corpus and the provider edge scripts.

A committed case holds ``$secret:`` references only. The scan flags Slack bot tokens, Telegram bot
tokens and the generated fake-secret marker wherever they appear in a committed case or an edge
script. A finding names the file and the shape, never the matched value. The scan never reads the
secret store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tests.contracts import corpus_v3
from tests.contracts.comms.fake_edge import EDGE_DIR

SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("slack_bot_token", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("generated_fake_secret", re.compile(r"corpus-fake-")),
)


@dataclass
class ScanReport:
    """What one corpus scan covered and what it flagged."""

    case_files: int
    edge_files: int
    wire_files: int = 0
    findings: list[str] = field(default_factory=list)


def scan_corpus() -> ScanReport:
    """Scan every v3 case file and every edge script."""
    manifest = corpus_v3.load_manifest(corpus_v3.V3_DIR)
    case_paths = [corpus_v3.V3_DIR / name for name in manifest["cases"]]
    edge_paths = sorted(EDGE_DIR.glob("*.json"))
    findings: list[str] = []
    for path in [*case_paths, *edge_paths]:
        text = path.read_text(encoding="utf-8")
        findings.extend(f"{path.name}: {shape}" for shape in find_credential_shapes(text))
    return ScanReport(case_files=len(case_paths), edge_files=len(edge_paths), findings=findings)


def find_credential_shapes(text: str) -> list[str]:
    """The name of each credential shape found in ``text``, one name per match."""
    return [name for name, pattern in SHAPES for _ in pattern.finditer(text)]
