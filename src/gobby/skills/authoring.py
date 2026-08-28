"""Authoring policy for Gobby-owned bundled skill instructions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gobby.config.skills import SkillsConfig
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase

BUNDLED_MAX_CONTENT_SIZE_KEY = "skills.bundled_max_content_size"

DECOMPOSITION_GUIDANCE = """Decompose this bundled instruction file:
1. Keep SKILL.md as purpose, common path, invariants, and topic index.
2. Move conditional detail to topic-named references.
3. Keep each reference within the same limit.
4. Provide the exact condition and get_skill_file call for every reference.
5. Split semantically rather than into numbered parts.
6. Keep normal workflows within a three-reference activation budget.
7. Preserve expected artifacts, validators, and recovery behavior."""


@dataclass(frozen=True, slots=True)
class BundledContentViolation:
    """One bundled instruction file above the configured authoring ceiling."""

    path: Path
    character_count: int
    byte_count: int
    limit: int

    @property
    def message(self) -> str:
        return (
            f"{self.path}: {self.character_count} characters, {self.byte_count} UTF-8 bytes; "
            f"configured limit {self.limit}. {DECOMPOSITION_GUIDANCE}"
        )


def resolve_bundled_max_content_size(db: HubDatabase) -> int:
    """Resolve the live authoring ceiling, including registered defaults."""
    value = (
        ConfigStore(db)
        .read_snapshot()
        .values.get(
            BUNDLED_MAX_CONTENT_SIZE_KEY,
            SkillsConfig().bundled_max_content_size,
        )
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return SkillsConfig().bundled_max_content_size
    return value


def find_bundled_content_violations(
    skills_root: Path,
    limit: int,
) -> list[BundledContentViolation]:
    """Check complete bundled entrypoints and references against both size measures."""
    violations: list[BundledContentViolation] = []
    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        candidates = [skill_dir / "SKILL.md"]
        references = skill_dir / "references"
        if references.is_dir():
            candidates.extend(sorted(path for path in references.rglob("*") if path.is_file()))
        for path in candidates:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            character_count = len(text)
            byte_count = len(text.encode("utf-8"))
            if character_count <= limit and byte_count <= limit:
                continue
            violations.append(
                BundledContentViolation(
                    path=path,
                    character_count=character_count,
                    byte_count=byte_count,
                    limit=limit,
                )
            )
    return violations
