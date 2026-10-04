#!/usr/bin/env python3
"""Refresh the vendored Trusted network seed from the Claude Code docs.

Prints a unified diff against the vendored seed; writes only with ``--write``.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

from gobby.agents.sandbox_domains import is_srt_domain_pattern

__all__ = ["SOURCE_URL", "is_srt_domain_pattern", "main", "parse_default_allowed_domains"]

SOURCE_URL = "https://code.claude.com/docs/en/cloud-environments.md#default-allowed-domains"
_SEED = Path(__file__).resolve().parents[1] / "src/gobby/data/sandbox/trusted_domains.json"
_HEADING = "## Default allowed domains"
_ACCORDION = re.compile(r'<Accordion title="([^"]+)">')
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _fetch() -> str:
    # The docs host answers urllib's default User-Agent with 403.
    request = urllib.request.Request(
        SOURCE_URL.split("#", 1)[0], headers={"User-Agent": "gobby-refresh-trusted-domains"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return str(response.read().decode("utf-8"))


def parse_default_allowed_domains(markdown: str) -> dict[str, list[str]]:
    """Return category title to domains from the ``Default allowed domains`` section."""
    _, found, section = markdown.partition(_HEADING)
    if not found:
        raise ValueError(f"missing {_HEADING!r} section")
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    categories: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in section.splitlines():
        line = line.strip()
        if title := _ACCORDION.fullmatch(line):
            current = categories.setdefault(title[1], [])
        elif line.startswith("* ") and current is not None:
            # Some bullets carry a trailing note such as "pub.dev (Dart/Flutter)".
            entry = _LINK.sub(r"\1", line[2:].strip()).split()[0]
            current.append(entry.replace("\\", ""))
    if not any(categories.values()):
        raise ValueError(f"no domains found under {_HEADING!r}")
    return categories


def _categories_text(categories: dict[str, list[str]]) -> list[str]:
    return (json.dumps(categories, indent=2) + "\n").splitlines(keepends=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the vendored seed")
    args = parser.parse_args(argv)

    categories = parse_default_allowed_domains(_fetch())
    domains = [domain for group in categories.values() for domain in group]
    offenders = [domain for domain in domains if not is_srt_domain_pattern(domain)]
    if offenders:
        print("Entries SRT would refuse:", *offenders, sep="\n  ", file=sys.stderr)
        return 1

    current: dict[str, list[str]] = {}
    if _SEED.exists():
        current = json.loads(_SEED.read_text(encoding="utf-8"))["categories"]
    sys.stdout.writelines(
        difflib.unified_diff(
            _categories_text(current),
            _categories_text(categories),
            fromfile=f"{_SEED.name} (vendored)",
            tofile=f"{_SEED.name} (fetched)",
        )
    )
    if args.write:
        seed = {
            "source_url": SOURCE_URL,
            "fetched_at": datetime.datetime.now(datetime.UTC).date().isoformat(),
            "sha256": hashlib.sha256(("\n".join(domains) + "\n").encode()).hexdigest(),
            "categories": categories,
        }
        _SEED.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
