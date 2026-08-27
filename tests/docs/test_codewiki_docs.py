"""Contract tests for maintained Codewiki guidance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/guides/codewiki.md"

ACTIVE_CODEWIKI_DOCS = (
    ROOT / "docs/contracts/gwiki-cli.md",
    ROOT / "docs/contracts/gcode-cli.md",
    ROOT / "docs/guides/ai-configuration.md",
    ROOT / "docs/guides/codewiki.md",
    ROOT / "docs/guides/gcode-user-guide.md",
    ROOT / "docs/guides/gcode-development-guide.md",
    ROOT / "docs/guides/gcore-development-guide.md",
    ROOT / "docs/guides/gwiki-user-guide.md",
    ROOT / "crates/gcode/README.md",
    ROOT / "src/gobby/install/shared/skills/code-index/SKILL.md",
    ROOT / "crates/gcode/assets/SKILL.md",
    ROOT / "crates/gwiki/src/commands/index.rs",
    ROOT / "crates/gwiki/src/vault.rs",
    ROOT / "crates/gcore/src/codewiki_contract.rs",
    ROOT / "crates/gcore/src/vault.rs",
    ROOT / "crates/gcore/src/vault/mermaid.rs",
    ROOT / "crates/gwiki/src/commands/code/repair.rs",
    ROOT / "crates/gwiki/src/commands/code/run.rs",
    ROOT / "crates/gwiki/src/commands/code/text/generation/tool_loop.rs",
    ROOT / "crates/gwiki/src/catalog.rs",
)

STALE_CODEWIKI_OWNERSHIP_PHRASES = (
    "gcode codewiki",
    "gcode's `codewiki`",
    "gcode (codewiki output)",
    "gcode's codewiki generator",
    "gcode contract freezes for `codewiki",
    "gcode `ToolExecutor` for the CodeWiki",
    # The persisted marker flipped to gwiki-code; active guidance may name the
    # legacy value only as history, which these docs phrase without the literal.
    "gcode-codewiki",
    # Command examples must carry the gwiki prefix.
    "`codewiki --repair-citations`",
    # The golden-fixture emitter moved to gwiki.
    "gcode pins its emitter",
    "pinned by gcode's emitter tests",
)


def test_codewiki_guide_uses_canonical_project_vault() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert ".gobby/wiki" not in guide
    assert "/path/to/project/wiki" in guide
    assert "<project-root>/wiki" in guide


def test_codewiki_guide_documents_paused_gwiki_code_contract() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "# gwiki CodeWiki Guide" in guide
    assert "`gwiki code`" in guide
    assert "isolated/manual use" in guide
    assert "operationally paused" in guide
    assert "`GET /api/wiki/code/status`" in guide
    assert '"state": "disabled"' in guide
    assert '"reason": "pending_wiki_redesign"' in guide
    assert "`POST /api/wiki/code/refresh`" in guide
    assert "409" in guide
    assert "`codewiki_disabled_pending_redesign`" in guide


def test_active_codewiki_surfaces_have_no_stale_gcode_invocation() -> None:
    missing_paths = [
        path.relative_to(ROOT).as_posix() for path in ACTIVE_CODEWIKI_DOCS if not path.exists()
    ]
    assert missing_paths == []

    normalized_phrases = [
        " ".join(phrase.split()).casefold() for phrase in STALE_CODEWIKI_OWNERSHIP_PHRASES
    ]
    stale_paths = [
        path.relative_to(ROOT).as_posix()
        for path in ACTIVE_CODEWIKI_DOCS
        if any(
            phrase in " ".join(path.read_text(encoding="utf-8").split()).casefold()
            for phrase in normalized_phrases
        )
    ]

    assert stale_paths == []
