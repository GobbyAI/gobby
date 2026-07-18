"""Contract tests for maintained Codewiki guidance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/guides/codewiki.md"


def test_codewiki_guide_uses_canonical_project_vault() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert ".gobby/wiki" not in guide
    assert "/path/to/project/wiki" in guide
    assert "<project-root>/wiki" in guide
