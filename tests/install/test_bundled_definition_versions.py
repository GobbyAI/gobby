"""Every bundled factory file carries a MAJOR.MINOR.PATCH version (AGENTS.md rule 17).

Enumerates each bundled family generically: rules, agents, pipelines, variables,
skill front matter, and skill references. No definition name appears here; an
offender is reported by its path at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from gobby.agents.sync import get_bundled_agents_path
from gobby.paths import get_install_dir
from gobby.skills.sync import get_bundled_skills_path
from gobby.utils.strict_yaml import load_strict_yaml
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path
from gobby.workflows.sync_rules import get_bundled_rules_paths
from gobby.workflows.sync_variables import get_bundled_variables_path

pytestmark = pytest.mark.unit

INSTALL_SHARED = get_install_dir() / "shared"
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
REFERENCE_VERSION_LINE = re.compile(r"_Version: [0-9]+\.[0-9]+\.[0-9]+_")
REFERENCE_FOOTER = "_Last verified"


def _semver_problem(value: object) -> str | None:
    if value is None:
        return "no version"
    if not isinstance(value, str) or SEMVER.fullmatch(value) is None:
        return f"malformed version {value!r}"
    return None


def _yaml_problem(path: Path) -> str | None:
    data = load_strict_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return "not a mapping"
    return _semver_problem(data.get("version"))


def _skill_problem(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return "no front matter"
    end = lines.index("---", 1)
    front_matter = load_strict_yaml("\n".join(lines[1:end]))
    if not isinstance(front_matter, dict):
        return "front matter is not a mapping"
    return _semver_problem(front_matter.get("version"))


def _reference_problem(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    version_indexes = [i for i, line in enumerate(lines) if line.startswith("_Version:")]
    if len(version_indexes) != 1:
        return f"expected one _Version line, found {len(version_indexes)}"
    version_index = version_indexes[0]
    if REFERENCE_VERSION_LINE.fullmatch(lines[version_index]) is None:
        return f"malformed version line {lines[version_index]!r}"
    footer_indexes = [i for i, line in enumerate(lines) if line.startswith(REFERENCE_FOOTER)]
    if footer_indexes and version_index > footer_indexes[0]:
        return "_Version line sits below _Last verified"
    return None


def _yaml_files(roots: list[Path]) -> list[Path]:
    return sorted({path for root in roots for path in root.rglob("*.yaml")})


def _families() -> dict[str, tuple[list[Path], Callable[[Path], str | None]]]:
    skills_root = get_bundled_skills_path()
    return {
        "rules": (_yaml_files(get_bundled_rules_paths()), _yaml_problem),
        "agents": (_yaml_files([get_bundled_agents_path()]), _yaml_problem),
        "pipelines": (_yaml_files([get_bundled_pipelines_path()]), _yaml_problem),
        "variables": (_yaml_files([get_bundled_variables_path()]), _yaml_problem),
        "skill front matter": (sorted(skills_root.glob("*/SKILL.md")), _skill_problem),
        "skill references": (
            [
                path
                for path in sorted(skills_root.rglob("*.md"))
                if path.name != "SKILL.md"
                and path.relative_to(skills_root).parts[1:2] == ("references",)
            ],
            _reference_problem,
        ),
    }


def test_every_bundled_factory_file_has_a_semver_version() -> None:
    offenders: list[str] = []
    for family, (paths, check) in _families().items():
        assert paths, f"no bundled {family} found; enumeration is vacuous"
        for path in paths:
            problem = check(path)
            if problem is not None:
                offenders.append(f"{family}: {path.relative_to(INSTALL_SHARED)}: {problem}")
    assert not offenders, "bundled files without a MAJOR.MINOR.PATCH version:\n" + "\n".join(
        offenders
    )
