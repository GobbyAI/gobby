"""Cross-artifact integrity guards for the bundled Impeccable release."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import cast

from gobby.skills.parser import parse_frontmatter
from gobby.utils.dependency_requirements import (
    IMPECCABLE_NODE_MIN_VERSION,
    IMPECCABLE_RELEASE,
)

PROJECT_ROOT = Path(__file__).parents[2]
SKILL_ROOT = PROJECT_ROOT / "src/gobby/install/shared/skills/impeccable"
REFERENCES_ROOT = SKILL_ROOT / "references"
MANAGED_LOCKFILE = PROJECT_ROOT / "src/gobby/install/impeccable-package-lock.json"

EXPECTED_DEPENDENCIES = {
    "css-select": "^7.0.0",
    "css-tree": "^3.2.1",
    "domutils": "^4.0.2",
    "fflate": "^0.8.3",
    "htmlparser2": "^12.0.0",
    "marked": "^18.0.5",
}
EXPECTED_OPTIONAL_DEPENDENCIES = {"puppeteer": "^25.1.0"}

RELEASE_REFERENCES = frozenset(
    {
        "adapt.md",
        "adapt.native.md",
        "android.md",
        "animate.md",
        "audit.md",
        "audit.native.md",
        "bolder.md",
        "clarify.md",
        "colorize.md",
        "craft-floor.md",
        "craft.md",
        "critique.md",
        "degraded/asset-producer.md",
        "degraded/documenter.md",
        "degraded/finish-reviewer.md",
        "degraded/manual-edit-applier.md",
        "delight.md",
        "distill.md",
        "doctor.md",
        "document.md",
        "extract.md",
        "harden.md",
        "hooks.md",
        "init.md",
        "ios.md",
        "layout.md",
        "live-setup.md",
        "live.md",
        "new-work.md",
        "onboard.md",
        "operate.md",
        "optimize.md",
        "overdrive.md",
        "polish.md",
        "quieter.md",
        "routing.md",
        "shape.md",
        "typeset.md",
        "visualize.md",
    }
)
GOBBY_RETAINED_REFERENCES = frozenset(
    {
        "color-and-contrast.md",
        "critique-cognitive-load.md",
        "critique-personas.md",
        "critique-report.md",
        "critique-scoring.md",
        "critique-workflow.md",
        "design-execution.md",
        "design-foundations.md",
        "interaction-design.md",
        "live-actions.md",
        "live-contract.md",
        "live-generation.md",
        "live-setup-recovery.md",
        "live-variants.md",
        "motion-design.md",
        "new-work-build.md",
        "new-work-direction.md",
        "new-work-finish.md",
        "new-work-invention.md",
        "responsive-design.md",
        "spatial-design.md",
        "teach.md",
        "typography.md",
        "ux-writing.md",
    }
)
DISPATCHABLE_REFERENCES = frozenset(
    {
        "adapt.md",
        "adapt.native.md",
        "animate.md",
        "audit.md",
        "audit.native.md",
        "bolder.md",
        "clarify.md",
        "colorize.md",
        "craft.md",
        "critique.md",
        "delight.md",
        "distill.md",
        "document.md",
        "extract.md",
        "harden.md",
        "init.md",
        "layout.md",
        "live.md",
        "onboard.md",
        "optimize.md",
        "overdrive.md",
        "polish.md",
        "quieter.md",
        "shape.md",
        "teach.md",
        "typeset.md",
    }
)

_REFRESHED_REFERENCES = {
    "adapt.md",
    "animate.md",
    "audit.md",
    "bolder.md",
    "clarify.md",
    "colorize.md",
    "craft-floor.md",
    "critique.md",
    "delight.md",
    "distill.md",
    "harden.md",
    "layout.md",
    "operate.md",
    "optimize.md",
    "overdrive.md",
    "polish.md",
    "quieter.md",
    "shape.md",
    "typeset.md",
}
_NAMED_DEFAULT_REFERENCES = {
    "craft.md",
    "document.md",
    "hooks.md",
    "init.md",
    "live-setup.md",
    "live.md",
    "new-work.md",
    "routing.md",
}
_STANDARD_REFERENCES = {"doctor.md", "extract.md", "onboard.md", "visualize.md"}
_NEAR_VERBATIM_REFERENCES = {
    "adapt.native.md",
    "android.md",
    "audit.native.md",
    "ios.md",
}
_VENDORED_AS_IS_REFERENCES = {
    "degraded/asset-producer.md",
    "degraded/documenter.md",
    "degraded/finish-reviewer.md",
    "degraded/manual-edit-applier.md",
}
EXPECTED_NOTICE_CLASSIFICATIONS = {
    **dict.fromkeys(_REFRESHED_REFERENCES, "refreshed-with-catalogued-adaptations"),
    **dict.fromkeys(_NAMED_DEFAULT_REFERENCES, "named-default-adapted"),
    **dict.fromkeys(_STANDARD_REFERENCES, "standard-adapted"),
    **dict.fromkeys(_NEAR_VERBATIM_REFERENCES, "near-verbatim"),
    **dict.fromkeys(_VENDORED_AS_IS_REFERENCES, "vendored-as-is"),
    **dict.fromkeys(GOBBY_RETAINED_REFERENCES, "gobby-retained-extra"),
}

_NOTICE_CLASS_PREFIXES = {
    "Refreshed with catalogued adaptations": "refreshed-with-catalogued-adaptations",
    "Named-default adaptation": "named-default-adapted",
    "Standard adaptation": "standard-adapted",
    "Near-verbatim native reference": "near-verbatim",
    "Vendored as-is": "vendored-as-is",
    "Gobby-retained upstream domain reference": "gobby-retained-extra",
    "Gobby-retained decomposition reference": "gobby-retained-extra",
}
_NOTICE_ROW = re.compile(r"^\| `references/(?P<path>[^`]+)` \| (?P<label>[^|]+) \|$", re.MULTILINE)
_REFERENCE_LINK = re.compile(r"`references/(?P<path>[^`]+\.md)`")
_UPSTREAM_LIFECYCLE_COMMAND = re.compile(
    r"(?<![\w-])(?:npx\s+)?impeccable\s+(?:install|update)\b",
    re.IGNORECASE,
)
_GOBBY_LIFECYCLE_ROUTES = {
    "SKILL.md": ("teach mode", "materialize_skill_scripts"),
    "references/doctor.md": ("gobby install", "repository re-vendor workflow"),
    "references/document.md": ("teach mode", ".impeccable.md"),
    "references/hooks.md": ("gobby install", "repository-only re-vendor workflow"),
    "references/init.md": ("gobby install", "teach mode"),
    "references/live-setup.md": ("materialize_skill_scripts",),
    "references/new-work.md": ("teach mode", ".impeccable.md"),
}


def _read_reference(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _notice_classification(label: str) -> str:
    for prefix, classification in _NOTICE_CLASS_PREFIXES.items():
        if label.startswith(prefix):
            return classification
    raise AssertionError(f"Unknown NOTICE classification: {label}")


def _notice_catalog(notice: str) -> dict[str, str]:
    return {
        match.group("path"): _notice_classification(match.group("label").strip())
        for match in _NOTICE_ROW.finditer(notice)
    }


def _dispatch_references(skill_body: str) -> set[str]:
    dispatch = skill_body.split("## Sub-command Dispatch", 1)[1].split("## Design Direction", 1)[0]
    table_rows = "\n".join(line for line in dispatch.splitlines() if line.startswith("| `"))
    return {match.group("path") for match in _REFERENCE_LINK.finditer(table_rows)}


def test_bundled_runtime_matches_release_pin() -> None:
    skill_text = _read_reference(SKILL_ROOT / "SKILL.md")
    frontmatter, skill_body = parse_frontmatter(skill_text)
    runtime = cast(dict[str, object], frontmatter["metadata"]["gobby"]["runtime"])
    scripts_package = cast(
        dict[str, object],
        json.loads(_read_reference(SKILL_ROOT / "scripts/package.json")),
    )

    assert IMPECCABLE_RELEASE.package == "impeccable"
    assert IMPECCABLE_RELEASE.version == "3.5.0"
    assert IMPECCABLE_NODE_MIN_VERSION == "22.12.0"
    assert runtime == {
        "node": f">={IMPECCABLE_NODE_MIN_VERSION}",
        "cli": {
            "npm": IMPECCABLE_RELEASE.package,
            "version": IMPECCABLE_RELEASE.version,
            "bin": "impeccable",
        },
        "skill_release": "4.0.4",
    }
    assert scripts_package["dependencies"] == EXPECTED_DEPENDENCIES
    assert scripts_package["optionalDependencies"] == EXPECTED_OPTIONAL_DEPENDENCIES
    assert hashlib.sha256(MANAGED_LOCKFILE.read_bytes()).hexdigest() == (
        IMPECCABLE_RELEASE.lockfile_sha256
    )

    actual_references = {
        path.relative_to(REFERENCES_ROOT).as_posix() for path in REFERENCES_ROOT.rglob("*.md")
    }
    assert actual_references - GOBBY_RETAINED_REFERENCES == RELEASE_REFERENCES
    assert actual_references == RELEASE_REFERENCES | GOBBY_RETAINED_REFERENCES
    assert _dispatch_references(skill_body) == DISPATCHABLE_REFERENCES
    assert set(EXPECTED_NOTICE_CLASSIFICATIONS) == actual_references
    assert _notice_catalog(_read_reference(SKILL_ROOT / "NOTICE.md")) == (
        EXPECTED_NOTICE_CLASSIFICATIONS
    )

    for relative_path in actual_references:
        reference = _read_reference(REFERENCES_ROOT / relative_path)
        if "node <scripts_dir>" in reference:
            assert 'materialize_skill_scripts(name="impeccable")' in reference, relative_path
            assert "environment.PUPPETEER_CACHE_DIR" in reference, relative_path


def test_vendored_tree_never_self_manages_upstream() -> None:
    lifecycle_documents = {"SKILL.md": _read_reference(SKILL_ROOT / "SKILL.md")}
    lifecycle_documents.update(
        {
            f"references/{path.relative_to(REFERENCES_ROOT).as_posix()}": _read_reference(path)
            for path in REFERENCES_ROOT.rglob("*.md")
        }
    )

    upstream_commands = {
        path: sorted(set(_UPSTREAM_LIFECYCLE_COMMAND.findall(body)))
        for path, body in lifecycle_documents.items()
        if _UPSTREAM_LIFECYCLE_COMMAND.search(body)
    }
    assert upstream_commands == {}

    for path, required_phrases in _GOBBY_LIFECYCLE_ROUTES.items():
        body = lifecycle_documents[path].lower()
        assert all(phrase.lower() in body for phrase in required_phrases), path
