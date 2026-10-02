"""Tests for the vendored Trusted network seed."""

from __future__ import annotations

import datetime
import hashlib
import json
from importlib.resources import files

from gobby.agents.sandbox_domains import is_srt_domain_pattern, trusted_domains

SOURCE_URL = "https://code.claude.com/docs/en/cloud-environments.md#default-allowed-domains"


def test_seed_provenance_and_entries_are_valid() -> None:
    seed = json.loads(
        files("gobby").joinpath("data/sandbox/trusted_domains.json").read_text(encoding="utf-8")
    )
    domains = [domain for group in seed["categories"].values() for domain in group]

    assert seed["source_url"] == SOURCE_URL
    datetime.date.fromisoformat(seed["fetched_at"])
    assert "Anthropic services" in seed["categories"]
    assert "api.anthropic.com" in domains
    assert seed["sha256"] == hashlib.sha256(("\n".join(domains) + "\n").encode()).hexdigest()
    assert trusted_domains() == tuple(domains)
    assert [domain for domain in domains if not is_srt_domain_pattern(domain)] == []
