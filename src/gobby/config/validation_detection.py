"""Configurable validation command detection."""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

PROJECT_VALIDATION_DETECTION_KEY = "validation_detection"
_SHELL_SEGMENT_SEPARATORS = {"&&", "||", ";", "|"}
_ENV_ASSIGNMENT_RE_PREFIX = "="
_MUTATING_VALIDATION_ARGS = ["--fix", "--unsafe-fixes", "--write", "-w"]


class ValidationCommandMatcher(BaseModel):
    """One editable validation command matcher."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable matcher id")
    label: str = Field(description="Human-readable matcher label")
    enabled: bool = True
    languages: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    prefixes: list[str] = Field(default_factory=list)
    required_args_all: list[str] = Field(default_factory=list)
    required_args_any: list[str] = Field(default_factory=list)
    forbidden_args_any: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("matcher id is required")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("matcher label is required")
        return value


class ValidationDetectionConfig(BaseModel):
    """Configuration for validation command recognition."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    builtin_matchers_enabled: bool = True
    disabled_builtin_matcher_ids: list[str] = Field(default_factory=list)
    recognized_wrappers: list[str] = Field(default_factory=list)
    custom_matchers: list[ValidationCommandMatcher] = Field(default_factory=list)


@dataclass(frozen=True)
class ValidationCommandMatch:
    """Result of validation command classification."""

    matcher_id: str
    label: str
    categories: tuple[str, ...]
    languages: tuple[str, ...]


def default_validation_wrappers() -> list[str]:
    """Return shell wrappers that should be ignored before command matching."""
    return [
        "uv run",
        "poetry run",
        "pipenv run",
        "pdm run",
        "hatch run",
        "rye run",
        "bundle exec",
        "npx",
        "pnpm exec",
        "yarn exec",
        "bunx",
    ]


def builtin_validation_matchers() -> list[ValidationCommandMatcher]:
    """Built-in matcher defaults for gcode-supported ecosystems."""
    return [
        _matcher(
            "python-tests",
            "Python tests",
            ["python"],
            ["test"],
            [
                "pytest",
                "python -m pytest",
                "python3 -m pytest",
                "coverage run",
                "python -m coverage run",
                "python3 -m coverage run",
            ],
        ),
        _matcher(
            "python-lint-type-format",
            "Python lint/type checks",
            ["python"],
            ["lint", "type_check"],
            [
                "ruff check",
                "python -m ruff check",
                "python3 -m ruff check",
                "pylint",
                "python -m pylint",
                "python3 -m pylint",
                "flake8",
                "python -m flake8",
                "python3 -m flake8",
                "mypy",
                "python -m mypy",
                "python3 -m mypy",
                "pyright",
                "basedpyright",
                "tox",
                "nox",
            ],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
        ),
        _matcher(
            "python-format-check",
            "Python format checks",
            ["python"],
            ["format"],
            [
                "ruff format",
                "python -m ruff format",
                "python3 -m ruff format",
                "black",
                "python -m black",
                "python3 -m black",
                "isort",
                "python -m isort",
                "python3 -m isort",
            ],
            required_args_any=["--check", "--check-only"],
        ),
        _matcher(
            "js-ts-tests",
            "JavaScript and TypeScript tests",
            ["javascript", "typescript"],
            ["test"],
            [
                "vitest",
                "jest",
                "npm test",
                "npm run test",
                "pnpm test",
                "pnpm run test",
                "yarn test",
                "yarn run test",
                "bun test",
                "bun run test",
                "deno test",
            ],
        ),
        _matcher(
            "js-ts-lint-type-format",
            "JavaScript and TypeScript lint/type/format",
            ["javascript", "typescript"],
            ["lint", "type_check", "format"],
            [
                "npm run lint",
                "npm run check",
                "npm run typecheck",
                "npm run type-check",
                "pnpm lint",
                "pnpm run lint",
                "pnpm check",
                "pnpm run check",
                "pnpm typecheck",
                "pnpm type-check",
                "pnpm run typecheck",
                "pnpm run type-check",
                "yarn lint",
                "yarn run lint",
                "yarn check",
                "yarn run check",
                "yarn typecheck",
                "yarn type-check",
                "yarn run typecheck",
                "yarn run type-check",
                "bun run lint",
                "bun run check",
                "bun run typecheck",
                "tsc",
                "vue-tsc",
                "svelte-check",
                "eslint",
                "stylelint",
                "biome check",
                "oxlint",
                "deno lint",
                "deno check",
            ],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
        ),
        _matcher(
            "js-ts-format-check",
            "JavaScript and TypeScript format checks",
            ["javascript", "typescript"],
            ["format"],
            ["prettier"],
            required_args_any=["--check", "--list-different"],
        ),
        _matcher(
            "go-validation",
            "Go tests and checks",
            ["go"],
            ["test", "lint", "type_check"],
            ["go test", "go vet", "golangci-lint run", "staticcheck"],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
        ),
        _matcher(
            "rust-validation",
            "Rust tests and checks",
            ["rust"],
            ["test", "lint", "type_check"],
            ["cargo test", "cargo nextest run", "cargo check", "cargo clippy"],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
        ),
        _matcher(
            "rust-format-check",
            "Rust format checks",
            ["rust"],
            ["format"],
            ["cargo fmt"],
            required_args_any=["--check"],
        ),
        _matcher(
            "java-kotlin-validation",
            "Java and Kotlin tests and checks",
            ["java", "kotlin"],
            ["test", "lint", "type_check"],
            [
                "mvn test",
                "mvn verify",
                "./mvnw test",
                "./mvnw verify",
                "gradle test",
                "gradle check",
                "./gradlew test",
                "./gradlew check",
                "ktlint",
            ],
        ),
        _matcher(
            "php-validation",
            "PHP tests and checks",
            ["php"],
            ["test", "lint"],
            ["phpunit", "vendor/bin/phpunit", "composer test", "phpstan", "psalm"],
        ),
        _matcher(
            "dart-validation",
            "Dart and Flutter tests and checks",
            ["dart"],
            ["test", "lint", "type_check"],
            ["dart test", "dart analyze", "flutter test", "flutter analyze"],
        ),
        _matcher(
            "csharp-validation",
            "C# tests and checks",
            ["csharp"],
            ["test", "lint", "type_check"],
            ["dotnet test", "dotnet build"],
        ),
        _matcher(
            "csharp-format-check",
            "C# format checks",
            ["csharp"],
            ["format"],
            ["dotnet format"],
            required_args_any=["--verify-no-changes"],
        ),
        _matcher(
            "c-cpp-validation",
            "C and C++ tests and checks",
            ["c", "cpp"],
            ["test", "lint", "type_check"],
            ["ctest", "cmake --build", "clang-tidy", "cppcheck"],
        ),
        _matcher(
            "elixir-validation",
            "Elixir tests and checks",
            ["elixir"],
            ["test", "lint"],
            ["mix test", "mix credo"],
        ),
        _matcher(
            "elixir-format-check",
            "Elixir format checks",
            ["elixir"],
            ["format"],
            ["mix format"],
            required_args_any=["--check-formatted"],
        ),
        _matcher(
            "ruby-validation",
            "Ruby tests and checks",
            ["ruby"],
            ["test", "lint"],
            ["rspec", "bundle exec rspec", "rake test", "rubocop"],
        ),
        _matcher(
            "swift-validation",
            "Swift tests and checks",
            ["swift"],
            ["test", "lint"],
            ["swift test", "swiftlint"],
        ),
        _matcher(
            "data-doc-validation",
            "Markdown YAML and JSON checks",
            ["markdown", "yaml", "json"],
            ["lint", "format"],
            ["markdownlint", "yamllint", "jq", "jsonlint"],
        ),
        _matcher(
            "prettier-format-check",
            "Prettier format checks",
            ["markdown", "yaml", "json", "javascript", "typescript"],
            ["format"],
            ["prettier"],
            required_args_any=["--check", "--list-different"],
        ),
        _matcher(
            "make-validation",
            "Make validation targets",
            [],
            ["test", "lint"],
            ["make test", "make tests", "make lint"],
        ),
    ]


def default_validation_detection_config() -> ValidationDetectionConfig:
    """Return default validation detection config."""
    return ValidationDetectionConfig(recognized_wrappers=default_validation_wrappers())


def classify_validation_command(
    command: Any,
    config: ValidationDetectionConfig | None = None,
) -> ValidationCommandMatch | None:
    """Return validation match metadata for a shell command."""
    if not isinstance(command, str) or not command.strip():
        return None

    detection_config = config or default_validation_detection_config()
    if not detection_config.enabled:
        return None

    for segment in _command_segments(command):
        normalized = _normalize_segment(segment, detection_config.recognized_wrappers)
        if not normalized:
            continue
        for matcher in _iter_matchers(detection_config):
            if _matcher_matches_segment(matcher, normalized):
                return ValidationCommandMatch(
                    matcher_id=matcher.id,
                    label=matcher.label,
                    categories=tuple(matcher.categories),
                    languages=tuple(matcher.languages),
                )
    return None


def is_validation_command(
    command: Any,
    config: ValidationDetectionConfig | None = None,
) -> bool:
    """Return whether a shell command is recognized as validation."""
    return classify_validation_command(command, config) is not None


def load_project_validation_detection(project_path: str | None) -> dict[str, Any] | None:
    """Load project validation detection override from `.gobby/project.json`."""
    if not project_path:
        return None
    project_file = Path(project_path) / ".gobby" / "project.json"
    if not project_file.exists():
        return None
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read validation detection config from %s: %s", project_file, exc)
        return None
    raw = data.get(PROJECT_VALIDATION_DETECTION_KEY)
    return raw if isinstance(raw, dict) else None


def save_project_validation_detection(
    project_path: str,
    config_data: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Write project validation detection override to `.gobby/project.json`."""
    project_root = Path(project_path)
    project_file = project_root / ".gobby" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_project_payload(project_path)
    if config_data is None:
        payload.pop(PROJECT_VALIDATION_DETECTION_KEY, None)
        saved = None
    else:
        validated = ValidationDetectionConfig.model_validate(config_data).model_dump()
        payload[PROJECT_VALIDATION_DETECTION_KEY] = validated
        saved = validated
    project_file.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return saved


def clear_project_validation_detection(project_path: str | None) -> None:
    """Remove project validation detection override."""
    if not project_path:
        return
    data = load_project_validation_detection(project_path)
    if data is None:
        return
    save_project_validation_detection(project_path, None)


def resolve_validation_detection_config(
    *,
    daemon_config: Any | None = None,
    project_path: str | None = None,
) -> ValidationDetectionConfig:
    """Resolve daemon defaults plus project overrides."""
    base = getattr(daemon_config, "validation_detection", None)
    if not isinstance(base, ValidationDetectionConfig):
        base = default_validation_detection_config()
    resolved = base.model_copy(deep=True)

    project_override = load_project_validation_detection(project_path)
    if not project_override:
        return resolved

    for field in ("enabled", "builtin_matchers_enabled"):
        if field in project_override:
            setattr(resolved, field, project_override[field])

    if "recognized_wrappers" in project_override:
        resolved.recognized_wrappers = _unique_strings(
            [*resolved.recognized_wrappers, *project_override.get("recognized_wrappers", [])]
        )
    if "disabled_builtin_matcher_ids" in project_override:
        resolved.disabled_builtin_matcher_ids = _unique_strings(
            [
                *resolved.disabled_builtin_matcher_ids,
                *project_override.get("disabled_builtin_matcher_ids", []),
            ]
        )
    if "custom_matchers" in project_override:
        resolved.custom_matchers = [
            *resolved.custom_matchers,
            *[
                ValidationCommandMatcher.model_validate(item)
                for item in project_override.get("custom_matchers", [])
            ],
        ]
    return resolved


def _matcher(
    matcher_id: str,
    label: str,
    languages: list[str],
    categories: list[str],
    prefixes: list[str],
    *,
    required_args_any: list[str] | None = None,
    required_args_any_for: dict[str, list[str]] | None = None,
    forbidden_args_any: list[str] | None = None,
) -> ValidationCommandMatcher:
    if not required_args_any_for:
        return ValidationCommandMatcher(
            id=matcher_id,
            label=label,
            languages=languages,
            categories=categories,
            prefixes=prefixes,
            required_args_any=required_args_any or [],
            forbidden_args_any=forbidden_args_any or [],
        )

    expanded_prefixes: list[str] = []
    for prefix in prefixes:
        required = required_args_any_for.get(prefix)
        if not required:
            expanded_prefixes.append(prefix)
            continue
        for arg in required:
            expanded_prefixes.append(f"{prefix} {arg}")
    return ValidationCommandMatcher(
        id=matcher_id,
        label=label,
        languages=languages,
        categories=categories,
        prefixes=expanded_prefixes,
        required_args_any=required_args_any or [],
        forbidden_args_any=forbidden_args_any or [],
    )


def _iter_matchers(config: ValidationDetectionConfig) -> Iterable[ValidationCommandMatcher]:
    disabled_ids = set(config.disabled_builtin_matcher_ids)
    if config.builtin_matchers_enabled:
        for matcher in builtin_validation_matchers():
            if matcher.enabled and matcher.id not in disabled_ids:
                yield matcher
    for matcher in config.custom_matchers:
        if matcher.enabled:
            yield matcher


def shell_command_segments(command: str) -> list[list[str]]:
    """Split a shell command into token segments separated by shell operators."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = []
    current: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SHELL_SEGMENT_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    return segments


_command_segments = shell_command_segments


def _normalize_segment(tokens: list[str], wrappers: list[str]) -> list[str]:
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return []
    changed = True
    while changed:
        changed = False
        gsqz_tokens = _strip_gsqz_wrapper(tokens)
        if gsqz_tokens is not None:
            tokens = _strip_env_assignments(gsqz_tokens)
            changed = True
            continue
        for wrapper in wrappers:
            wrapper_tokens = _safe_split(wrapper)
            if wrapper_tokens and _starts_with(tokens, wrapper_tokens):
                tokens = tokens[len(wrapper_tokens) :]
                if wrapper_tokens == ["uv", "run"]:
                    tokens = _strip_uv_run_options(tokens)
                tokens = _strip_env_assignments(tokens)
                changed = True
                break
    return tokens


def _strip_gsqz_wrapper(tokens: list[str]) -> list[str] | None:
    if len(tokens) < 2 or Path(tokens[0]).name != "gsqz" or tokens[1] != "--":
        return None
    if len(tokens) == 2:
        return []
    if len(tokens) == 3:
        return _safe_split(tokens[2])
    return tokens[2:]


def _matcher_matches_segment(matcher: ValidationCommandMatcher, tokens: list[str]) -> bool:
    if not matcher.prefixes:
        return False
    for prefix in matcher.prefixes:
        prefix_tokens = _safe_split(prefix)
        if not prefix_tokens or not _starts_with(tokens, prefix_tokens):
            continue
        if any(_tokens_include_arg(tokens, arg) for arg in matcher.forbidden_args_any):
            continue
        if matcher.required_args_all and not all(
            _tokens_include_arg(tokens, arg) for arg in matcher.required_args_all
        ):
            continue
        if matcher.required_args_any and not any(
            _tokens_include_arg(tokens, arg) for arg in matcher.required_args_any
        ):
            continue
        return True
    return False


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and _looks_like_env_assignment(tokens[index]):
        index += 1
    return tokens[index:]


def _strip_uv_run_options(tokens: list[str]) -> list[str]:
    index = 0
    options_with_values = {"--project", "--cache-dir", "--python", "-p", "-C"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if not token.startswith("-"):
            return tokens[index:]
        index += 2 if token in options_with_values else 1
    return []


def _tokens_include_arg(tokens: list[str], arg: str) -> bool:
    return any(token == arg or token.startswith(f"{arg}=") for token in tokens)


def _looks_like_env_assignment(token: str) -> bool:
    if _ENV_ASSIGNMENT_RE_PREFIX not in token:
        return False
    name, _, _ = token.partition("=")
    return bool(name) and name.replace("_", "").isalnum() and not name[0].isdigit()


def _safe_split(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _starts_with(tokens: list[str], prefix: list[str]) -> bool:
    if len(tokens) < len(prefix):
        return False
    return tokens[: len(prefix)] == prefix


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result


def _load_project_payload(project_path: str) -> dict[str, Any]:
    project_file = Path(project_path) / ".gobby" / "project.json"
    if not project_file.exists():
        return {}
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
