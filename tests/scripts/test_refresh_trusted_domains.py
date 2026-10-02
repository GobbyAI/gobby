"""Tests for the reviewed Trusted seed refresh script."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import refresh_trusted_domains as refresh

MARKDOWN = """# Cloud environments

* ignored.example.com

## Default allowed domains

Domains marked with `*` match subdomains.

<AccordionGroup>
  <Accordion title="Version control">
    * github.com
    * [www.github.com](http://www.github.com)
    * raw\\.githubusercontent.com
  </Accordion>

  <Accordion title="Container registries">
    * \\*.gcr.io
    * pub.dev (Dart/Flutter)
  </Accordion>
</AccordionGroup>

## Next section

* also-ignored.example.com
"""


def test_parse_diff_and_write_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert refresh.parse_default_allowed_domains(MARKDOWN) == {
        "Version control": ["github.com", "www.github.com", "raw.githubusercontent.com"],
        "Container registries": ["*.gcr.io", "pub.dev"],
    }
    for refused in (
        "*",
        "*.com",
        "https://example.com",
        "example.com:443",
        "example.com/p",
        "example .com",
        "example..com",
        "*.exa mple.com",
    ):
        assert not refresh.is_srt_domain_pattern(refused)
    assert refresh.is_srt_domain_pattern("localhost")
    assert refresh.is_srt_domain_pattern("[::1]")

    seed = tmp_path / "trusted_domains.json"
    monkeypatch.setattr(refresh, "_SEED", seed)

    monkeypatch.setattr(refresh, "_fetch", lambda: MARKDOWN.replace("github.com\n", "*.com\n", 1))
    assert refresh.main(["--write"]) == 1
    assert "*.com" in capsys.readouterr().err
    assert not seed.exists()

    monkeypatch.setattr(refresh, "_fetch", lambda: MARKDOWN)
    assert refresh.main([]) == 0
    assert '+    "www.github.com",' in capsys.readouterr().out
    assert not seed.exists()

    assert refresh.main(["--write"]) == 0
    written = json.loads(seed.read_text(encoding="utf-8"))
    domains = "github.com\nwww.github.com\nraw.githubusercontent.com\n*.gcr.io\npub.dev\n"
    assert written["source_url"] == refresh.SOURCE_URL
    assert written["sha256"] == hashlib.sha256(domains.encode()).hexdigest()
    assert list(written["categories"]) == ["Version control", "Container registries"]
