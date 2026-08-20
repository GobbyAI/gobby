"""Tests for project verification refresh."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from gobby.ai.text_generation import FeatureGenerationUnavailableError, TextGenerationRequest
from gobby.config.features import ProjectVerificationSynthesisConfig
from gobby.project_verification import refresh as refresh_module
from gobby.project_verification.candidates import (
    _is_frontend_command,
    _package_script_command,
    generate_candidates,
    is_safe_validation_command,
    select_best_candidates,
    verification_dict_from_candidates,
)
from gobby.project_verification.evidence import (
    FRONTEND_SUBDIRS,
    MAX_FILE_BYTES,
    EvidenceBundle,
    _split_run_commands,
    collect_evidence,
)
from gobby.project_verification.refresh import (
    ProjectVerificationAIError,
    ProjectVerificationReadError,
    refresh_project_verification,
    refresh_project_verification_deterministic,
)
from gobby.project_verification.synthesis import (
    PROJECT_VERIFICATION_SCHEMA,
    synthesize_verification_commands,
)

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

    assert result.after["unit_tests"] == "pytest tests/ -v"
    assert result.after["type_check"] == "mypy src/ --no-incremental --strict"
    assert result.after["lint"] == "ruff check src/"
    assert result.after["format"] == "ruff format --check src/"
    assert result.after["custom"]["frontend_tests"] == "cd web && npm test"
    assert result.after["custom"]["frontend_lint"] == "cd web && npm run lint"
    assert result.after["custom"]["ts_check"] == "cd web && npm run type-check"


@pytest.mark.parametrize(
    ("lockfile", "runner", "build_command"),
    [
        ("uv.lock", "uv run", "uv build"),
        ("poetry.lock", "poetry run", "poetry build"),
        ("pdm.lock", "pdm run", "pdm build"),
    ],
)
def test_python_lockfile_selects_runner_and_build_command(
    tmp_path: Path,
    lockfile: str,
    runner: str,
    build_command: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='example'\n[build-system]\nrequires=[]\n",
        encoding="utf-8",
    )
    (tmp_path / lockfile).touch()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["unit_tests"] == f"{runner} pytest tests/ -v"
    assert result.after["type_check"] == f"{runner} mypy src/"
    assert result.after["lint"] == f"{runner} ruff check src/"
    assert result.after["format"] == f"{runner} ruff format --check src/"
    assert result.after["build"] == build_command
    assert "GOBBY_TEST_PROTECT" not in result.after["unit_tests"]


def test_python_lockfile_precedence_is_uv_then_poetry_then_pdm(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='example'\n",
        encoding="utf-8",
    )
    for lockfile in ("pdm.lock", "poetry.lock", "uv.lock"):
        (tmp_path / lockfile).touch()
    (tmp_path / "tests").mkdir()

    bundle = collect_evidence(tmp_path)
    result = refresh_project_verification_deterministic(tmp_path)

    assert bundle.python is not None
    assert bundle.python.package_manager == "uv"
    assert result.after["unit_tests"] == "uv run pytest tests/ -v"


def test_python_without_lockfile_uses_direct_tools_without_gobby_prefix(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='example'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    bundle = collect_evidence(tmp_path)
    result = refresh_project_verification_deterministic(tmp_path)

    assert bundle.python is not None
    assert bundle.python.package_manager is None
    assert result.after["unit_tests"] == "pytest tests/ -v"
    assert "GOBBY_TEST_PROTECT" not in result.after["unit_tests"]


def test_gobby_project_name_enables_test_protection_only_with_positive_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='gobby'\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").touch()
    (tmp_path / "tests").mkdir()

    result = refresh_project_verification_deterministic(tmp_path)

    assert result.after["unit_tests"] == "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v"


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


def test_short_deliberate_existing_command_survives_ci_refresh(tmp_path: Path) -> None:
    project_json_path = write_project_json(tmp_path, {"unit_tests": "pytest -x"})
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

    result = refresh_project_verification_deterministic(tmp_path, fix=True)
    persisted = json.loads(project_json_path.read_text(encoding="utf-8"))

    assert result.after["unit_tests"] == "pytest -x"
    assert persisted["verification"]["unit_tests"] == "pytest -x"


@pytest.mark.parametrize(
    ("command", "expected_confidence"),
    [("pytest", 0.62), ("pytest -x", 0.82), ("uv run pytest tests/ -v", 0.84)],
)
def test_existing_evidence_and_candidate_share_confidence_policy(
    tmp_path: Path,
    command: str,
    expected_confidence: float,
) -> None:
    write_project_json(tmp_path, {"unit_tests": command})

    bundle = collect_evidence(tmp_path)
    existing_item = next(item for item in bundle.items if item.kind == "existing")
    existing_candidate = next(
        candidate
        for candidate in generate_candidates(bundle)
        if candidate.source_kind == "existing"
    )

    assert existing_item.confidence == expected_confidence
    assert existing_candidate.confidence == expected_confidence


def test_ci_replaces_weak_existing_generic_command(tmp_path: Path) -> None:
    project_json_path = write_project_json(tmp_path, {"unit_tests": "pytest"})
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

    bundle = collect_evidence(tmp_path)
    candidates = generate_candidates(bundle)
    result = refresh_project_verification_deterministic(tmp_path, fix=True)
    persisted = json.loads(project_json_path.read_text(encoding="utf-8"))

    unit_test_candidates = [candidate for candidate in candidates if candidate.name == "unit_tests"]
    assert [
        (candidate.source_kind, candidate.confidence) for candidate in unit_test_candidates
    ] == [
        ("existing", 0.62),
        ("ci", 0.82),
    ]
    assert select_best_candidates(unit_test_candidates)["unit_tests"].source_kind == "ci"
    assert result.after["unit_tests"] == "uv run pytest tests/ -v"
    assert persisted["verification"]["unit_tests"] == "uv run pytest tests/ -v"


def test_split_run_commands_joins_backslash_continuations() -> None:
    run = "uv run pytest \\\n  --cov=gobby tests/unit"

    assert _split_run_commands(run) == ["uv run pytest --cov=gobby tests/unit"]


@pytest.mark.parametrize(
    "run",
    [
        pytest.param("uv run pytest \\", id="single-backslash"),
        pytest.param(r"uv run pytest \\", id="double-backslash"),
    ],
)
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


def test_unreadable_makefile_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "Makefile").mkdir()
    (tmp_path / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  build:\n    cmds:\n      - go build ./...\n",
        encoding="utf-8",
    )

    bundle = collect_evidence(tmp_path)

    assert any(
        item.source == "Taskfile.yml" and item.command == "go build ./..." for item in bundle.items
    )


class FakeJSONService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.request: TextGenerationRequest | None = None

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.request = request
        return self.payload


class FailingJSONService:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        raise self.error


class UnexpectedSynthesisFailure(BaseException):
    pass


@pytest.mark.asyncio
async def test_async_refresh_offloads_evidence_collection_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project_json(tmp_path, {})
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    event_loop_thread = threading.get_ident()
    evidence_threads: list[int] = []
    write_threads: list[int] = []
    collect_evidence_original = refresh_module.collect_evidence
    write_verification_original = refresh_module._write_verification

    def collect_evidence_recording(root: Path) -> EvidenceBundle:
        evidence_threads.append(threading.get_ident())
        return collect_evidence_original(root)

    def write_verification_recording(path: Path, verification: dict[str, Any]) -> None:
        write_threads.append(threading.get_ident())
        write_verification_original(path, verification)

    monkeypatch.setattr(refresh_module, "collect_evidence", collect_evidence_recording)
    monkeypatch.setattr(refresh_module, "_write_verification", write_verification_recording)

    result = await refresh_project_verification(tmp_path, fix=True, ai_mode="off")

    assert result.written is True
    assert evidence_threads and evidence_threads[0] != event_loop_thread
    assert write_threads and write_threads[0] != event_loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [FeatureGenerationUnavailableError("service unavailable"), ValueError("invalid JSON")],
)
async def test_async_refresh_auto_falls_back_for_expected_synthesis_errors(
    tmp_path: Path, error: Exception
) -> None:
    write_project_json(tmp_path, {})

    result = await refresh_project_verification(
        tmp_path,
        ai_mode="auto",
        text_generation_service=FailingJSONService(error),
    )

    assert result.ai_error == str(error)
    assert result.ai_used is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [FeatureGenerationUnavailableError("service unavailable"), ValueError("invalid JSON")],
)
async def test_async_refresh_ai_on_wraps_expected_synthesis_errors(
    tmp_path: Path, error: Exception
) -> None:
    write_project_json(tmp_path, {})

    with pytest.raises(ProjectVerificationAIError, match="AI verification synthesis failed"):
        await refresh_project_verification(
            tmp_path,
            ai_mode="on",
            text_generation_service=FailingJSONService(error),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [asyncio.CancelledError, TypeError, UnexpectedSynthesisFailure]
)
async def test_async_refresh_propagates_unexpected_synthesis_failures(
    tmp_path: Path, error_type: type[BaseException]
) -> None:
    write_project_json(tmp_path, {})
    error = error_type("unexpected")

    with pytest.raises(error_type) as caught:
        await refresh_project_verification(
            tmp_path,
            ai_mode="auto",
            text_generation_service=FailingJSONService(error),
        )

    assert caught.value is error


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
                    "command": "pytest tests/ -v",
                    "confidence": 0.91,
                    "sources": ["pyproject.toml"],
                    "rationale": "tests directory exists",
                }
            }
        }
    )
    config = ProjectVerificationSynthesisConfig(candidates=["endpoint:lm-studio/test-model"])

    result = await synthesize_verification_commands(service, config, bundle, candidates)

    assert result.accepted["unit_tests"].command == "pytest tests/ -v"
    assert service.request is not None
    assert service.request.profile == "feature_mid"
    assert service.request.candidates == tuple(config.candidates)
    assert service.request.caller == "project_verification.refresh"
    assert service.request.json_schema == PROJECT_VERIFICATION_SCHEMA


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
        ProjectVerificationSynthesisConfig(candidates=["endpoint:lm-studio/test-model"]),
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
    assert is_safe_validation_command("ruff check --fix-only src", slot="lint") is False
    assert is_safe_validation_command("ruff check --unsafe-fixes src", slot="lint") is False
    assert is_safe_validation_command("prettier --write=src", slot="format") is False
    assert is_safe_validation_command("npm run format:check", slot="format") is True
    assert is_safe_validation_command("yarn format:check", slot="format") is True


@pytest.mark.parametrize("subdir", FRONTEND_SUBDIRS)
def test_frontend_command_uses_shared_frontend_subdirectories(subdir: str) -> None:
    assert _is_frontend_command(f"cd {subdir} && cargo clippy") is True


def test_frontend_command_requires_frontend_root_for_non_node_tool() -> None:
    assert _is_frontend_command("cd backend && cargo clippy") is False


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


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="list"),
        pytest.param("not-an-object", id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_non_object_project_json_is_ignored_with_warning(
    tmp_path: Path,
    payload: object,
) -> None:
    gobby_dir = tmp_path / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")

    bundle = collect_evidence(tmp_path)

    assert bundle.existing_verification == {}
    assert bundle.items == []
    assert bundle.existing_project_json_intact is False
    assert len(bundle.warnings) == 1
    assert "top-level JSON value is not an object" in bundle.warnings[0]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="list"),
        pytest.param("not-an-object", id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_non_object_package_json_is_ignored_with_warning(
    tmp_path: Path,
    payload: object,
) -> None:
    (tmp_path / "package.json").write_text(json.dumps(payload), encoding="utf-8")

    bundle = collect_evidence(tmp_path)

    assert bundle.packages == []
    assert bundle.items == []
    assert len(bundle.warnings) == 1
    assert "top-level JSON value is not an object" in bundle.warnings[0]


def test_taskfile_case_variants_collect_one_physical_file_once(tmp_path: Path) -> None:
    (tmp_path / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - go test ./...\n",
        encoding="utf-8",
    )

    bundle = collect_evidence(tmp_path)

    taskfile_items = [
        item
        for item in bundle.items
        if item.source.lower() == "taskfile.yml" and item.command == "go test ./..."
    ]
    assert len(taskfile_items) == 1


def test_oversized_taskfile_case_variants_warn_once_for_one_physical_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "Taskfile.yml").write_text(
        "x" * (MAX_FILE_BYTES + 1),
        encoding="utf-8",
    )

    bundle = collect_evidence(tmp_path)

    taskfile_warnings = [
        warning for warning in bundle.warnings if "taskfile.yml" in warning.lower()
    ]
    assert len(taskfile_warnings) == 1
    assert "exceeds MAX_FILE_BYTES" in taskfile_warnings[0]


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


def test_write_verification_preserves_existing_project_json_mode(tmp_path: Path) -> None:
    project_json = write_project_json(tmp_path, {})
    project_json.chmod(0o640)

    refresh_module._write_verification(project_json, {"unit_tests": "pytest"})

    assert stat.S_IMODE(project_json.stat().st_mode) == 0o640


def test_write_verification_uses_secure_mode_for_new_project_json(tmp_path: Path) -> None:
    project_json = tmp_path / ".gobby" / "project.json"

    refresh_module._write_verification(project_json, {"unit_tests": "pytest"})

    assert stat.S_IMODE(project_json.stat().st_mode) == 0o600


def test_write_verification_backs_up_corrupt_file_and_cleans_temp_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_json = tmp_path / ".gobby" / "project.json"
    project_json.parent.mkdir()
    corrupt_content = b"{not-json\n"
    project_json.write_bytes(corrupt_content)

    original_replace = os.replace

    def fail_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(destination) == project_json:
            raise OSError("replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        refresh_module._write_verification(project_json, {"unit_tests": "pytest"})

    assert project_json.with_suffix(".json.bak").read_bytes() == corrupt_content
    assert list(project_json.parent.glob(".project.json.*.tmp")) == []
