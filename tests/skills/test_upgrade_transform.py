from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

TRANSFORM_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "impeccable"
    / ".upgrade"
    / "transform.py"
)


def _load_transform() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gobby_impeccable_upgrade", TRANSFORM_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_fixture_repo(repo_root: Path) -> None:
    install_dir = repo_root / "src" / "gobby" / "install"
    skill_dir = install_dir / "shared" / "skills" / "impeccable"
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"
    upgrade_dir = skill_dir / ".upgrade"

    scripts_dir.mkdir(parents=True)
    references_dir.mkdir()
    upgrade_dir.mkdir()
    (scripts_dir / "old.mjs").write_text("old script\n", encoding="utf-8")
    _write_json(
        scripts_dir / "package.json",
        {
            "name": "gobby-impeccable-scripts",
            "private": True,
            "type": "module",
            "version": "4.0.4",
            "dependencies": {"old": "1.0.0"},
            "optionalDependencies": {"old-optional": "1.0.0"},
        },
    )
    (scripts_dir / "package-lock.json").write_text("old scripts lock\n", encoding="utf-8")

    (references_dir / "bolder.md").write_text("curated bolder adaptation\n", encoding="utf-8")
    (references_dir / "onboard.md").write_text("old onboard\n", encoding="utf-8")
    (references_dir / "retained.md").write_text("retained domain reference\n", encoding="utf-8")
    (references_dir / "degraded").mkdir()
    (references_dir / "degraded" / "asset-producer.md").write_text(
        "old degraded\n", encoding="utf-8"
    )
    (upgrade_dir / "README.md").write_text("repository only\n", encoding="utf-8")

    (skill_dir / "SKILL.md").write_text(
        """---
name: impeccable
metadata:
  gobby:
    runtime:
      node: \">=22.18.0\"
      cli:
        npm: \"impeccable\"
        version: \"3.5.0\"
        bin: \"impeccable\"
      skill_release: \"4.0.4\"
---

Curated Gobby skill body.
""",
        encoding="utf-8",
    )
    (skill_dir / "NOTICE.md").write_text(
        """# Notice

## Upstream

- Script release: 4.0.4, staged 2026-08-08 through the release channel with
  Gobby's pinned `impeccable@3.5.0` CLI.

### Reference catalogue

| Reference | Classification |
|-----------|----------------|
| `references/bolder.md` | Refreshed with catalogued adaptations |
| `references/onboard.md` | Standard adaptation |
| `references/degraded/asset-producer.md` | Vendored as-is |
| `references/retained.md` | Gobby-retained upstream domain reference |
""",
        encoding="utf-8",
    )

    dependency_requirements = repo_root / "src" / "gobby" / "utils"
    dependency_requirements.mkdir(parents=True)
    (dependency_requirements / "dependency_requirements.py").write_text(
        """IMPECCABLE_RELEASE = ImpeccableRelease(
    package=\"impeccable\",
    version=\"3.5.0\",
    lockfile_sha256=\"old-hash\",
)
IMPECCABLE_NODE_MIN_VERSION = \"22.18.0\"
""",
        encoding="utf-8",
    )
    (install_dir / "impeccable-package-lock.json").write_text(
        "old managed lock\n", encoding="utf-8"
    )
    _write_json(
        install_dir / "bundled_content_manifest.json",
        {
            "schema_version": 1,
            "hash_algorithm": "sha256",
            "root": "shared",
            "files": {},
        },
    )


def _snapshot(repo_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repo_root).as_posix(): path.read_bytes()
        for path in sorted(repo_root.rglob("*"))
        if path.is_file()
    }


def _make_stage_provider(
    module: ModuleType,
    *,
    resolved_version: str = "3.6.0",
    engine: str | None = ">=23.1",
    package_name: str = "impeccable",
) -> Callable[[str, Path], object]:
    def stage(candidate_version: str, workspace: Path) -> object:
        package_dir = workspace / "node_modules" / "impeccable"
        skill_dir = workspace / "generated" / "impeccable"
        package_dir.mkdir(parents=True)
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "reference" / "degraded").mkdir(parents=True)

        package_json: dict[str, object] = {
            "name": package_name,
            "version": resolved_version,
            "dependencies": {"css-tree": "^3.3.0"},
            "optionalDependencies": {"puppeteer": "^26.0.0"},
        }
        if engine is not None:
            package_json["engines"] = {"node": engine}
        _write_json(package_dir / "package.json", package_json)

        (skill_dir / "SKILL.md").write_text(
            "---\nname: impeccable\nversion: 4.1.0\n---\n\nUpstream body.\n",
            encoding="utf-8",
        )
        (skill_dir / "scripts" / "new.mjs").write_text(
            "export const release = '4.1.0';\n", encoding="utf-8"
        )
        (skill_dir / "reference" / "bolder.md").write_text(
            "# New upstream bolder\n", encoding="utf-8"
        )
        (skill_dir / "reference" / "onboard.md").write_text(
            """> **Additional context needed**: the desired result.

# Onboard

Run `node {{scripts_path}}/inspect.mjs`, then continue with [polish.md](polish.md).
Use DESIGN.md and {{config_file}}. {{ask_instruction}} before changing direction.
Available commands: {{available_commands}}.
""",
            encoding="utf-8",
        )
        (skill_dir / "reference" / "degraded" / "asset-producer.md").write_text(
            "released degraded\n", encoding="utf-8"
        )
        return module.StagedCandidate(package_dir=package_dir, skill_dir=skill_dir)

    return stage


def _fake_lockfile(package_json: bytes, _workspace: Path) -> bytes:
    package = json.loads(package_json)
    return (
        json.dumps(
            {
                "name": package["name"],
                "lockfileVersion": 3,
                "packages": {"": package},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def test_transform_mini_release_idempotent(tmp_path: Path) -> None:
    module = _load_transform()
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    original = _snapshot(repo_root)
    stage_calls: list[str] = []

    def must_not_stage(candidate_version: str, workspace: Path) -> object:
        stage_calls.append(candidate_version)
        return _make_stage_provider(module)(candidate_version, workspace)

    with pytest.raises(SystemExit) as omitted:
        module.main([])
    assert omitted.value.code == 2

    for invalid_version in ("3.6", "3.6.0-beta.1", "^3.6.0", "latest"):
        with pytest.raises(module.UpgradeRejected, match="exact MAJOR.MINOR.PATCH"):
            module.upgrade(
                repo_root,
                invalid_version,
                stage_provider=must_not_stage,
                lockfile_builder=_fake_lockfile,
            )
    assert stage_calls == []
    assert _snapshot(repo_root) == original

    mismatched_stage = _make_stage_provider(module, resolved_version="3.6.1")
    with pytest.raises(module.UpgradeRejected, match="resolved version 3.6.1"):
        module.upgrade(
            repo_root,
            "3.6.0",
            stage_provider=mismatched_stage,
            lockfile_builder=_fake_lockfile,
        )
    assert _snapshot(repo_root) == original

    for invalid_engine in (None, "23.1", "^23.1.0", ">=23.1 <24", ">=23.1.0-beta.1"):
        with pytest.raises(module.UpgradeRejected, match="engines.node"):
            module.upgrade(
                repo_root,
                "3.6.0",
                stage_provider=_make_stage_provider(module, engine=invalid_engine),
                lockfile_builder=_fake_lockfile,
            )
        assert _snapshot(repo_root) == original

    report = module.upgrade(
        repo_root,
        "3.6.0",
        stage_provider=_make_stage_provider(module),
        lockfile_builder=_fake_lockfile,
    )

    skill_dir = repo_root / "src" / "gobby" / "install" / "shared" / "skills" / "impeccable"
    scripts_dir = skill_dir / "scripts"
    assert {
        path.relative_to(scripts_dir).as_posix()
        for path in scripts_dir.rglob("*")
        if path.is_file()
    } == {
        "new.mjs",
        "package-lock.json",
        "package.json",
    }
    assert (scripts_dir / "new.mjs").read_text(encoding="utf-8") == (
        "export const release = '4.1.0';\n"
    )
    scripts_package = json.loads((scripts_dir / "package.json").read_text(encoding="utf-8"))
    assert scripts_package == {
        "name": "gobby-impeccable-scripts",
        "private": True,
        "type": "module",
        "version": "4.1.0",
        "dependencies": {"css-tree": "^3.3.0"},
        "optionalDependencies": {"puppeteer": "^26.0.0"},
    }
    assert (scripts_dir / "package-lock.json").read_bytes() == _fake_lockfile(
        (scripts_dir / "package.json").read_bytes(), tmp_path
    )

    references_dir = skill_dir / "references"
    assert (references_dir / "bolder.md").read_text() == "curated bolder adaptation\n"
    assert (references_dir / "retained.md").read_text() == "retained domain reference\n"
    assert (references_dir / "degraded" / "asset-producer.md").read_text() == (
        "released degraded\n"
    )
    onboard = (references_dir / "onboard.md").read_text(encoding="utf-8")
    assert onboard.startswith(
        "> You are continuing a session under the `impeccable` skill; "
        "the design-context protocol and anti-pattern rules already apply. "
        "Additional context needed: the desired result.\n"
    )
    assert "node <scripts_dir>/inspect.mjs" in onboard
    assert 'materialize_skill_scripts(name="impeccable")' in onboard
    assert "environment.PUPPETEER_CACHE_DIR" in onboard
    assert 'get_skill_file(name="impeccable", path="references/polish.md")' in onboard
    assert "DESIGN.md" not in onboard
    assert ".impeccable.md" in onboard
    assert "{{" not in onboard

    skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert 'node: ">=23.1.0"' in skill
    assert 'version: "3.6.0"' in skill
    assert 'skill_release: "4.1.0"' in skill
    notice = (skill_dir / "NOTICE.md").read_text(encoding="utf-8")
    assert "Script release: 4.1.0" in notice
    assert "`impeccable@3.6.0` CLI" in notice

    dependency_text = (
        repo_root / "src" / "gobby" / "utils" / "dependency_requirements.py"
    ).read_text(encoding="utf-8")
    managed_lock = (
        repo_root / "src" / "gobby" / "install" / "impeccable-package-lock.json"
    ).read_bytes()
    assert 'version="3.6.0"' in dependency_text
    assert 'IMPECCABLE_NODE_MIN_VERSION = "23.1.0"' in dependency_text
    assert f'lockfile_sha256="{hashlib.sha256(managed_lock).hexdigest()}"' in dependency_text
    assert b'"impeccable": "3.6.0"' in managed_lock

    manifest = json.loads(
        (repo_root / "src" / "gobby" / "install" / "bundled_content_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["files"]
    assert all(
        not any(part.startswith(".") for part in Path(relative_path).parts)
        for relative_path in manifest["files"]
    )
    assert any("bolder.md" in item for item in report.judgment_needed)
    assert any("Node engine floor" in item for item in report.judgment_needed)

    first_result = _snapshot(repo_root)
    second_report = module.upgrade(
        repo_root,
        "3.6.0",
        stage_provider=_make_stage_provider(module),
        lockfile_builder=_fake_lockfile,
    )
    assert _snapshot(repo_root) == first_result
    assert second_report.changed_paths == ()
