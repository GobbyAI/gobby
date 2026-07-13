"""Tests for project verification refresh."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.ai.text_generation import TextGenerationRequest
from gobby.config.features import ProjectVerificationSynthesisConfig
from gobby.project_verification.candidates import (
    _package_script_command,
    generate_candidates,
    is_safe_validation_command,
    select_best_candidates,
    verification_dict_from_candidates,
)
from gobby.project_verification.evidence import (
    MAX_FILE_BYTES,
    _split_run_commands,
    collect_evidence,
)
from gobby.project_verification.refresh import (
    ProjectVerificationReadError,
    refresh_project_verification_deterministic,
)
from gobby.project_verification.synthesis import synthesize_verification_commands

pytestmark = [pytest.mark.unit]


def write_project_json(root: Path, verification: dict[str, Any]) -> Path:
    gobby_dir = root / ".gobby"
    gobby_dir.mkdir()
    project_json = gobby_dir / "project.json"
    project_json.write_text(
        json.dumps(
            {
                "id": "proj-1",
                "name": "example",
                "created_at": "2026-01-01T00:00:00Z",
                "verification": verification,
            }
        ),
        encoding="utf-8",
    )
    return project_json


def test_python_and_node_evidence_generates_expected_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='example'\n[tool.mypy]\nstrict=true\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest run",
                    "lint": "eslint .",
                    "type-check": "tsc --noEmit",
                }
            }
        ),
        encoding="utf-8",
    )

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["unit_tests"] == "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v"
    assert result.after["type_check"] == "uv run mypy src/ --no-incremental --strict"
    assert result.after["lint"] == "uv run ruff check src/"
    assert result.after["format"] == "uv run ruff format --check src/"
    assert result.after["custom"]["frontend_tests"] == "cd web && npm test"
    assert result.after["custom"]["frontend_lint"] == "cd web && npm run lint"
    assert result.after["custom"]["ts_check"] == "cd web && npm run type-check"


def test_rust_nextest_build_and_doc_tests_are_detected(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    nextest_dir = tmp_path / ".config"
    nextest_dir.mkdir()
    (nextest_dir / "nextest.toml").write_text("[profile.default]\n", encoding="utf-8")

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["unit_tests"] == "cargo nextest run"
    assert result.after["build"] == "cargo build"
    assert result.after["doc_tests"] == "cargo test --doc"
    assert result.after["format"] == "cargo fmt --check"
    assert result.after["lint"] == "cargo clippy"


def test_rich_existing_rust_commands_survive_weaker_manifest_detection(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    write_project_json(
        tmp_path,
        {
            "build": "cargo build --workspace --no-default-features",
            "unit_tests": "cargo nextest run --workspace --no-default-features",
            "doc_tests": "cargo test --doc --workspace --no-default-features",
            "format": "cargo fmt --check",
            "lint": "cargo clippy --workspace --no-default-features -- -D warnings",
        },
    )

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["build"] == "cargo build --workspace --no-default-features"
    assert result.after["unit_tests"] == "cargo nextest run --workspace --no-default-features"
    assert result.after["doc_tests"] == "cargo test --doc --workspace --no-default-features"
    assert result.after["lint"] == "cargo clippy --workspace --no-default-features -- -D warnings"


def test_ci_replaces_weak_existing_generic_command(tmp_path: Path) -> None:
    write_project_json(tmp_path, {"unit_tests": "pytest"})
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        """
jobs:
  test:
    steps:
      - name: Run tests
        run: uv run pytest tests/ -v
""",
        encoding="utf-8",
    )

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["unit_tests"] == "uv run pytest tests/ -v"


def test_split_run_commands_joins_backslash_continuations() -> None:
    run = "uv run pytest \\\n  --cov=gobby tests/unit"

    assert _split_run_commands(run) == ["uv run pytest --cov=gobby tests/unit"]


@pytest.mark.parametrize("run", ["uv run pytest \\", r"uv run pytest \\"])
def test_split_run_commands_rejects_trailing_backslash(run: str) -> None:
    assert _split_run_commands(run) == []


def test_ci_backslash_continuation_preserves_command_flags(tmp_path: Path) -> None:
    project_json_path = write_project_json(tmp_path, {"unit_tests": "pytest"})
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: Run tests\n"
        "        run: |\n"
        "          uv run pytest \\\n"
        "            --cov=gobby tests/unit\n",
        encoding="utf-8",
    )

    result = refresh_project_verification_deterministic(tmp_path, fix=True)
    persisted = json.loads(project_json_path.read_text(encoding="utf-8"))

    assert result.written
    assert result.after["unit_tests"] == "uv run pytest --cov=gobby tests/unit"
    assert persisted["verification"]["unit_tests"] == result.after["unit_tests"]
    assert not persisted["verification"]["unit_tests"].endswith("\\")


def test_recipe_ci_and_doc_evidence_generate_candidates(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\tgo test ./...\n", encoding="utf-8")
    (tmp_path / "Justfile").write_text("lint:\n    cargo clippy\n", encoding="utf-8")
    (tmp_path / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  build:\n    cmds:\n      - go build ./...\n",
        encoding="utf-8",
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        """
jobs:
  web:
    steps:
      - name: TypeScript
        working-directory: web
        run: npx tsc --noEmit
""",
        encoding="utf-8",
    )
    docs = tmp_path / "docs" / "guides"
    docs.mkdir(parents=True)
    (docs / "testing.md").write_text("Use `uv run pytest tests/api -v`.\n", encoding="utf-8")

    bundle = collect_evidence(tmp_path)
    selected = select_best_candidates(generate_candidates(bundle))
    verification = verification_dict_from_candidates(selected)

    assert verification["unit_tests"] == "go test ./..."
    assert verification["lint"] == "cargo clippy"
    assert verification["build"] == "go build ./..."
    assert verification["custom"]["ts_check"] == "cd web && npx tsc --noEmit"


class FakeJSONService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.request: TextGenerationRequest | None = None

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.request = request
        return self.payload


@pytest.mark.asyncio
async def test_synthesis_uses_profile_candidates_and_accepts_evidenced_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    bundle = collect_evidence(tmp_path)
    candidates = generate_candidates(bundle)
    service = FakeJSONService(
        {
            "commands": {
                "unit_tests": {
                    "command": "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v",
                    "confidence": 0.91,
                    "sources": ["pyproject.toml"],
                    "rationale": "tests directory exists",
                }
            }
        }
    )
    config = ProjectVerificationSynthesisConfig(candidates=["local:lm-studio/test-model"])

    result = await synthesize_verification_commands(service, config, bundle, candidates)

    assert result.accepted["unit_tests"].command == "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v"
    assert service.request is not None
    assert service.request.profile == "feature_mid"
    assert service.request.candidates == tuple(config.candidates)
    assert service.request.caller == "project_verification.refresh"


@pytest.mark.asyncio
async def test_synthesis_rejects_unsupported_or_mutating_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    bundle = collect_evidence(tmp_path)
    candidates = generate_candidates(bundle)
    service = FakeJSONService(
        {
            "commands": {
                "lint": {
                    "command": "uv run ruff check src/ --fix",
                    "confidence": 0.99,
                    "sources": ["pyproject.toml"],
                    "rationale": "bad command",
                },
                "unit_tests": {
                    "command": "python -m pytest generated",
                    "confidence": 0.99,
                    "sources": ["none"],
                    "rationale": "not a candidate",
                },
            }
        }
    )

    result = await synthesize_verification_commands(
        service,
        ProjectVerificationSynthesisConfig(candidates=["local:lm-studio/test-model"]),
        bundle,
        candidates,
    )

    assert not result.accepted
    assert {item.reason for item in result.rejected} == {
        "mutating validation command",
        "command lacks deterministic evidence",
    }


def test_safe_validation_command_uses_complete_tokens_for_format_scripts() -> None:
    assert is_safe_validation_command("npm run format", slot="format") is False
    assert is_safe_validation_command("cd web && npm run format", slot="format") is False
    assert is_safe_validation_command("eslint --fix=true src", slot="lint") is False
    assert is_safe_validation_command("prettier --write=src", slot="format") is False
    assert is_safe_validation_command("npm run format:check", slot="format") is True
    assert is_safe_validation_command("yarn format:check", slot="format") is True


@pytest.mark.parametrize(
    "command",
    [
        "pytest; id",
        "pytest && id",
        "pytest || id",
        "pytest | id",
        "pytest `id`",
        "pytest $(id)",
        "pytest > /tmp/out",
        "pytest < input",
        "pytest &",
        "pytest\nid",
        "pytest >> /tmp/out",
        "pytest 2>&1",
        "pytest <(id)",
        "npm run 'lint; id'",
        "id pytest",
        "cd web && id",
        "cd web && npm test && id",
        "cd ../web && npm test",
        "cd /tmp && npm test",
    ],
)
def test_safe_validation_command_rejects_shell_control_and_expansion(command: str) -> None:
    assert is_safe_validation_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "cd web && npm test",
        "cd 'web app' && npx tsc --noEmit",
        "cd 'web&app' && npm test",
        "cd 'web;app' && npm test",
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v",
    ],
)
def test_safe_validation_command_allows_curated_executables(command: str) -> None:
    assert is_safe_validation_command(command) is True


def test_package_script_command_quotes_script_argument() -> None:
    assert _package_script_command(".", "test command") == "npm run 'test command'"
    assert _package_script_command(".", "lint; id") == "npm run 'lint; id'"
    assert _package_script_command(".", "lint's") == "npm run 'lint'\"'\"'s'"
    assert _package_script_command("web app", "lint") == "cd 'web app' && npm run lint"


def test_hostile_ci_command_is_not_persisted(tmp_path: Path) -> None:
    project_json_path = write_project_json(tmp_path, {"unit_tests": "pytest"})
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'hostile-example'\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: Hostile tests\n"
        "        run: pytest && id > /tmp/pwn\n",
        encoding="utf-8",
    )

    bundle = collect_evidence(tmp_path)
    candidates = generate_candidates(bundle)
    result = refresh_project_verification_deterministic(tmp_path, fix=True)
    persisted = json.loads(project_json_path.read_text(encoding="utf-8"))

    assert all("/tmp/pwn" not in candidate.command for candidate in candidates)
    assert result.written
    assert persisted["verification"]["unit_tests"] == result.after["unit_tests"]
    assert "/tmp/pwn" not in persisted["verification"]["unit_tests"]


def test_oversized_structured_evidence_is_skipped_with_warnings(tmp_path: Path) -> None:
    paths = [
        tmp_path / "pyproject.toml",
        tmp_path / "package.json",
        tmp_path / "Taskfile.yml",
        tmp_path / ".github" / "workflows" / "ci.yml",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * (MAX_FILE_BYTES + 1), encoding="utf-8")

    bundle = collect_evidence(tmp_path)

    assert len(bundle.warnings) == len(paths)
    for path in paths:
        relative_path = path.relative_to(tmp_path).as_posix()
        assert any(relative_path in warning for warning in bundle.warnings)
    assert all("exceeds MAX_FILE_BYTES" in warning for warning in bundle.warnings)
    assert bundle.python is None
    assert bundle.packages == []


def test_fix_refuses_oversized_project_json_without_losing_user_commands(
    tmp_path: Path,
) -> None:
    command = "uv run pytest tests/custom -q"
    project_json = write_project_json(tmp_path, {"unit_tests": command})
    payload = json.loads(project_json.read_text(encoding="utf-8"))
    payload["large_user_config"] = "x" * (70 * 1024)
    project_json.write_text(json.dumps(payload), encoding="utf-8")
    original = project_json.read_bytes()

    preview = refresh_project_verification_deterministic(tmp_path)

    assert preview.warnings
    assert "exceeds MAX_FILE_BYTES" in preview.warnings[0]
    with pytest.raises(ProjectVerificationReadError, match="Refusing to update"):
        refresh_project_verification_deterministic(tmp_path, fix=True)
    assert project_json.read_bytes() == original
    persisted = json.loads(project_json.read_text(encoding="utf-8"))
    assert persisted["verification"]["unit_tests"] == command
