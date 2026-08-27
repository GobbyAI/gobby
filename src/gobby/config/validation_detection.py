"""Configurable validation command detection."""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.config.shell_lexing import parse_shell_command, safe_split

logger = logging.getLogger(__name__)

PROJECT_VALIDATION_DETECTION_KEY = "validation_detection"
_ENV_ASSIGNMENT_RE_PREFIX = "="
_MUTATING_VALIDATION_ARGS = ["--fix", "--unsafe-fixes", "--write", "-w"]
_NON_EXECUTING_VALIDATION_ARGS = [
    "--collect-only",
    "--co",
    "--version",
    "-V",
    "--help",
    "-h",
    "--fixtures",
    "--markers",
    "--dry-run",
]
_SELECTOR_VALIDATION_ARGS = ["-k", "-m", "--run", "-run", "--filter"]
_MAX_WRAPPER_NORMALIZATION_DEPTH = 8
# Every entry consumes the token after it, so an option missing here is read as
# the command and the run goes uncredited. List both forms of a spelling pair;
# `-C` is uv's `--config-setting`, and `--directory` has no short form.
_UV_RUN_OPTIONS_WITH_VALUES = [
    "--cache-dir",
    "--color",
    "--config-setting",
    "--directory",
    "--env-file",
    "--extra",
    "--group",
    "--package",
    "--project",
    "--python",
    "--with",
    "--with-editable",
    "--with-requirements",
    "-C",
    "-p",
    "-w",
]
WrapperKind = Literal["prefix", "delimiter", "command_string"]


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
    non_executing_args_any: list[str] = Field(default_factory=list)
    evidence_weakening_args_any: list[str] = Field(default_factory=list)
    evidence_weakening_bare_args_after: list[str] = Field(default_factory=list)

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


class ValidationCommandWrapper(BaseModel):
    """One command wrapper that exposes an inner validation command."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable wrapper id")
    label: str = Field(description="Human-readable wrapper label")
    prefixes: list[str] = Field(default_factory=list)
    kind: WrapperKind = "prefix"
    delimiter: str = "--"
    strip_options_with_values: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("wrapper id is required")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("wrapper label is required")
        return value


class ValidationDetectionConfig(BaseModel):
    """Configuration for validation command recognition."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    builtin_matchers_enabled: bool = True
    disabled_builtin_matcher_ids: list[str] = Field(default_factory=list)
    recognized_wrappers: list[str] = Field(default_factory=list)
    wrapper_rules: list[ValidationCommandWrapper] = Field(default_factory=list)
    custom_matchers: list[ValidationCommandMatcher] = Field(default_factory=list)


@dataclass(frozen=True)
class ValidationCommandMatch:
    """Result of validation command classification."""

    matcher_id: str
    label: str
    categories: tuple[str, ...]
    languages: tuple[str, ...]
    command: str = ""
    normalized_command: str = ""
    normalized_argv: tuple[str, ...] = ()
    wrapper_chain: tuple[str, ...] = ()
    segment_index: int = 0
    segment_count: int = 1
    shell_operators: tuple[str, ...] = ()
    evidence_requires_confirmation: bool = False

    @property
    def is_compound(self) -> bool:
        """Return whether aggregate shell status cannot prove this segment passed."""
        return self.segment_count > 1 or bool(self.shell_operators)


@dataclass(frozen=True)
class _NormalizedCommandSegment:
    """Normalized argv and wrapper metadata for one shell segment."""

    argv: tuple[str, ...]
    wrapper_chain: tuple[str, ...]
    shell_operators: tuple[str, ...] = ()


def default_validation_wrappers() -> list[str]:
    """Return shell wrappers that should be ignored before command matching."""
    return [
        "uv run",
        "rtk",
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


def default_validation_wrapper_rules() -> list[ValidationCommandWrapper]:
    """Return default wrapper normalization rules."""
    return [
        _wrapper_rule(
            "uv-run",
            "uv run",
            "prefix",
            ["uv run"],
            strip_options_with_values=_UV_RUN_OPTIONS_WITH_VALUES,
        ),
        _wrapper_rule("rtk", "rtk", "prefix", ["rtk"]),
        _wrapper_rule("poetry-run", "poetry run", "prefix", ["poetry run"]),
        _wrapper_rule("pdm-run", "pdm run", "prefix", ["pdm run"]),
        _wrapper_rule("pipenv-run", "pipenv run", "prefix", ["pipenv run"]),
        _wrapper_rule("bundle-exec", "bundle exec", "prefix", ["bundle exec"]),
        _wrapper_rule("pnpm-exec", "pnpm exec", "prefix", ["pnpm exec"]),
        _wrapper_rule("npx", "npx", "prefix", ["npx"]),
        _wrapper_rule("bunx", "bunx", "prefix", ["bunx"]),
        _wrapper_rule("timeout", "timeout", "delimiter", ["timeout"]),
        _wrapper_rule("env", "env", "delimiter", ["env"]),
        _wrapper_rule("command", "command", "delimiter", ["command"]),
        _wrapper_rule("nice", "nice", "delimiter", ["nice"]),
        _wrapper_rule("rust-token-killer", "rust-token-killer", "delimiter", ["rust-token-killer"]),
        _wrapper_rule(
            "rust-token-killer-command-string",
            "rust-token-killer command string",
            "command_string",
            ["rust-token-killer --"],
        ),
        _wrapper_rule("bash-c", "bash -c", "command_string", ["bash -c"]),
        _wrapper_rule("bash-lc", "bash -lc", "command_string", ["bash -lc"]),
        _wrapper_rule("sh-c", "sh -c", "command_string", ["sh -c"]),
        _wrapper_rule("zsh-c", "zsh -c", "command_string", ["zsh -c"]),
        _wrapper_rule("fish-c", "fish -c", "command_string", ["fish -c"]),
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
            evidence_weakening_args_any=_SELECTOR_VALIDATION_ARGS,
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
            forbidden_args_any=[*_MUTATING_VALIDATION_ARGS, "--install-types"],
        ),
        ValidationCommandMatcher(
            id="gobby-test-types-audit",
            label="Gobby test-types ratchet",
            languages=["python"],
            categories=["type_check"],
            prefixes=["gobby test-types audit"],
            required_args_all=["--baseline", "--fail-on-new"],
            non_executing_args_any=_NON_EXECUTING_VALIDATION_ARGS,
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
                "playwright test",
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
            evidence_weakening_args_any=["-run"],
        ),
        _matcher(
            "rust-validation",
            "Rust tests and checks",
            ["rust"],
            ["test", "lint", "type_check"],
            ["cargo test", "cargo nextest run", "cargo check", "cargo clippy"],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
            evidence_weakening_bare_args_after=["cargo test", "cargo nextest run"],
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
            ["markdownlint", "yamllint", "jsonlint"],
        ),
        _matcher(
            "json-jq-validation",
            "JSON validation with jq",
            ["json"],
            ["lint"],
            ["jq empty", "jq -e", "jq --exit-status"],
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
            "git-diff-check",
            "Git whitespace checks",
            [],
            ["lint"],
            ["git diff"],
            required_args_any=["--check"],
            forbidden_args_any=["--output", "--ext-diff", "--textconv"],
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
    """Return validation match metadata for the first validation segment of a command."""
    return next(_iter_validation_matches(command, config), None)


def classify_validation_segments(
    command: Any,
    config: ValidationDetectionConfig | None = None,
) -> tuple[ValidationCommandMatch, ...]:
    """Return one match per validation segment of a possibly compound command.

    A stash-wrapped test run (``git stash push src/x.py``, ``pytest tests/x.py``,
    ``git stash pop``) yields only the pytest segment, so consumers that scope
    paths to what was validated never read git or shell segments as targets.
    """
    return tuple(_iter_validation_matches(command, config))


def _iter_validation_matches(
    command: Any,
    config: ValidationDetectionConfig | None,
) -> Iterator[ValidationCommandMatch]:
    if not isinstance(command, str) or not command.strip():
        return

    detection_config = config or default_validation_detection_config()
    if not detection_config.enabled:
        return

    wrapper_rules = _iter_wrapper_rules(detection_config)
    parsed = parse_shell_command(command)
    for segment_index, segment in enumerate(parsed.segments):
        normalized_segments = _normalize_segments(list(segment), wrapper_rules)
        for nested_index, normalized in enumerate(normalized_segments):
            if not normalized.argv:
                continue
            for matcher in _iter_matchers(detection_config):
                if _matcher_matches_segment(matcher, list(normalized.argv)):
                    shell_operators = (*parsed.operators, *normalized.shell_operators)
                    yield ValidationCommandMatch(
                        matcher_id=matcher.id,
                        label=matcher.label,
                        categories=tuple(matcher.categories),
                        languages=tuple(matcher.languages),
                        command=command,
                        normalized_command=shlex.join(normalized.argv),
                        normalized_argv=normalized.argv,
                        wrapper_chain=normalized.wrapper_chain,
                        segment_index=segment_index + nested_index,
                        segment_count=len(parsed.segments) + len(normalized.shell_operators),
                        shell_operators=shell_operators,
                        evidence_requires_confirmation=(
                            _matcher_requires_execution_confirmation(matcher, list(normalized.argv))
                        ),
                    )
                    break


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
    if "wrapper_rules" in project_override:
        resolved.wrapper_rules = [
            *resolved.wrapper_rules,
            *[
                ValidationCommandWrapper.model_validate(item)
                for item in project_override.get("wrapper_rules", [])
            ],
        ]
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
    evidence_weakening_args_any: list[str] | None = None,
    evidence_weakening_bare_args_after: list[str] | None = None,
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
            non_executing_args_any=_NON_EXECUTING_VALIDATION_ARGS,
            evidence_weakening_args_any=evidence_weakening_args_any or [],
            evidence_weakening_bare_args_after=evidence_weakening_bare_args_after or [],
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
        non_executing_args_any=_NON_EXECUTING_VALIDATION_ARGS,
        evidence_weakening_args_any=evidence_weakening_args_any or [],
        evidence_weakening_bare_args_after=evidence_weakening_bare_args_after or [],
    )


def _wrapper_rule(
    wrapper_id: str,
    label: str,
    kind: WrapperKind,
    prefixes: list[str],
    *,
    delimiter: str = "--",
    strip_options_with_values: list[str] | None = None,
) -> ValidationCommandWrapper:
    return ValidationCommandWrapper(
        id=wrapper_id,
        label=label,
        kind=kind,
        prefixes=prefixes,
        delimiter=delimiter,
        strip_options_with_values=strip_options_with_values or [],
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


def _iter_wrapper_rules(config: ValidationDetectionConfig) -> list[ValidationCommandWrapper]:
    rules = [*default_validation_wrapper_rules(), *config.wrapper_rules]
    for wrapper in _unique_strings(config.recognized_wrappers):
        wrapper_tokens = safe_split(wrapper)
        rules.append(
            ValidationCommandWrapper(
                id=f"recognized-wrapper-{_wrapper_id_suffix(wrapper)}",
                label=f"Recognized wrapper: {wrapper}",
                kind="prefix",
                prefixes=[wrapper],
                strip_options_with_values=(
                    _UV_RUN_OPTIONS_WITH_VALUES if wrapper_tokens == ["uv", "run"] else []
                ),
            )
        )
    return rules


def _normalize_segments(
    tokens: list[str],
    wrappers: list[ValidationCommandWrapper],
    *,
    wrapper_chain: tuple[str, ...] = (),
    shell_operators: tuple[str, ...] = (),
    depth: int = 0,
) -> list[_NormalizedCommandSegment]:
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return [_NormalizedCommandSegment((), wrapper_chain, shell_operators)]
    if depth >= _MAX_WRAPPER_NORMALIZATION_DEPTH:
        return [_NormalizedCommandSegment(tuple(tokens), wrapper_chain, shell_operators)]

    applied = _apply_wrapper_rule(tokens, wrappers)
    if applied is None:
        return [_NormalizedCommandSegment(tuple(tokens), wrapper_chain, shell_operators)]

    unwrapped_segments, wrapper_id, nested_operators = applied
    normalized: list[_NormalizedCommandSegment] = []
    for unwrapped in unwrapped_segments:
        normalized.extend(
            _normalize_segments(
                unwrapped,
                wrappers,
                wrapper_chain=(*wrapper_chain, wrapper_id),
                shell_operators=(*shell_operators, *nested_operators),
                depth=depth + 1,
            )
        )
    return normalized


def _apply_wrapper_rule(
    tokens: list[str],
    wrappers: list[ValidationCommandWrapper],
) -> tuple[list[list[str]], str, tuple[str, ...]] | None:
    matches = sorted(
        _matching_wrapper_prefixes(tokens, wrappers),
        key=lambda match: (-len(match[2]), match[0]),
    )
    for _, wrapper, prefix_tokens in matches:
        normalized = _unwrap_matched_rule(tokens, wrapper, prefix_tokens)
        if normalized is not None:
            unwrapped, shell_operators = normalized
            return unwrapped, wrapper.id, shell_operators
    return None


def _matching_wrapper_prefixes(
    tokens: list[str],
    wrappers: list[ValidationCommandWrapper],
) -> Iterable[tuple[int, ValidationCommandWrapper, list[str]]]:
    for index, wrapper in enumerate(wrappers):
        for prefix in wrapper.prefixes:
            prefix_tokens = safe_split(prefix)
            if prefix_tokens and _starts_with_command_prefix(tokens, prefix_tokens):
                yield index, wrapper, prefix_tokens


def _unwrap_matched_rule(
    tokens: list[str],
    wrapper: ValidationCommandWrapper,
    prefix_tokens: list[str],
) -> tuple[list[list[str]], tuple[str, ...]] | None:
    if wrapper.kind == "prefix":
        remaining = tokens[len(prefix_tokens) :]
        if wrapper.strip_options_with_values:
            remaining = _strip_wrapper_options(remaining, set(wrapper.strip_options_with_values))
        return [remaining], ()

    if wrapper.kind == "delimiter":
        try:
            delimiter_index = tokens.index(wrapper.delimiter, len(prefix_tokens))
        except ValueError:
            return None
        return [tokens[delimiter_index + 1 :]], ()

    command_tokens = tokens[len(prefix_tokens) :]
    if not command_tokens:
        return [[]], ()
    if len(command_tokens) == 1:
        parsed = parse_shell_command(command_tokens[0])
        return [list(segment) for segment in parsed.segments], parsed.operators
    return [command_tokens], ()


def _matcher_matches_segment(matcher: ValidationCommandMatcher, tokens: list[str]) -> bool:
    if not matcher.prefixes:
        return False
    for prefix in matcher.prefixes:
        prefix_tokens = safe_split(prefix)
        if not prefix_tokens or not _starts_with(tokens, prefix_tokens):
            continue
        if any(_tokens_include_arg(tokens, arg) for arg in matcher.forbidden_args_any):
            continue
        if any(_tokens_include_arg(tokens, arg) for arg in matcher.non_executing_args_any):
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


def _matcher_requires_execution_confirmation(
    matcher: ValidationCommandMatcher,
    tokens: list[str],
) -> bool:
    prefix_lengths = [
        len(prefix_tokens)
        for prefix in matcher.prefixes
        if (prefix_tokens := safe_split(prefix)) and _starts_with(tokens, prefix_tokens)
    ]
    arguments = tokens[max(prefix_lengths, default=0) :]
    if any(_tokens_include_arg(arguments, arg) for arg in matcher.evidence_weakening_args_any):
        return True
    for prefix in matcher.evidence_weakening_bare_args_after:
        prefix_tokens = safe_split(prefix)
        if not _starts_with(tokens, prefix_tokens):
            continue
        remaining = tokens[len(prefix_tokens) :]
        if remaining and not remaining[0].startswith("-"):
            return True
    return False


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and _looks_like_env_assignment(tokens[index]):
        index += 1
    return tokens[index:]


def _strip_wrapper_options(tokens: list[str], options_with_values: set[str]) -> list[str]:
    index = 0
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


def _starts_with_command_prefix(tokens: list[str], prefix: list[str]) -> bool:
    if len(tokens) < len(prefix):
        return False
    return _matches_command_token(tokens[0], prefix[0]) and tokens[1 : len(prefix)] == prefix[1:]


def _matches_command_token(token: str, expected: str) -> bool:
    return token == expected or ("/" not in expected and Path(token).name == expected)


def _starts_with(tokens: list[str], prefix: list[str]) -> bool:
    if len(tokens) < len(prefix):
        return False
    return tokens[: len(prefix)] == prefix


def _wrapper_id_suffix(wrapper: str) -> str:
    pieces = safe_split(wrapper) or [wrapper]
    suffix = "-".join(pieces)
    suffix = "".join(char if char.isalnum() else "-" for char in suffix).strip("-")
    return suffix or "custom"


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
