"""Validation command matchers: the editable matcher model and built-in tool knowledge."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ValidationCommandMatcher(BaseModel):
    """One editable validation command matcher."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable matcher id")
    label: str = Field(description="Human-readable matcher label")
    enabled: bool = True
    languages: list[str] = Field(default_factory=list)
    #: The command reads only ``languages`` code plus data and unindexed files, so a
    #: later edit in another indexed code language cannot change its result.
    bounded_inputs: bool = False
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

    @model_validator(mode="after")
    def validate_bounded_inputs(self) -> ValidationCommandMatcher:
        """Bounded inputs are only meaningful against the languages they bound."""
        if self.bounded_inputs and not self.languages:
            raise ValueError("bounded_inputs requires languages")
        return self


def builtin_validation_matchers() -> list[ValidationCommandMatcher]:
    """Built-in matcher defaults for gcode-supported ecosystems.

    Only direct checkers opt into ``bounded_inputs``. Test runners, package scripts,
    task runners, builds, and prettier plugins can read files in any language.
    """
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
            "python-lint-type",
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
            ],
            forbidden_args_any=[*_MUTATING_VALIDATION_ARGS, "--install-types"],
            bounded_inputs=True,
        ),
        _matcher(
            "python-task-runners",
            "Python task runner checks",
            ["python"],
            ["lint", "type_check"],
            ["tox", "nox"],
            forbidden_args_any=[*_MUTATING_VALIDATION_ARGS, "--install-types"],
        ),
        ValidationCommandMatcher(
            id="gobby-test-types-audit",
            label="Gobby test-types ratchet",
            languages=["python"],
            bounded_inputs=True,
            categories=["type_check"],
            prefixes=["gobby test-types audit"],
            required_args_all=["--baseline", "--fail-on-new"],
            non_executing_args_any=_NON_EXECUTING_VALIDATION_ARGS,
        ),
        ValidationCommandMatcher(
            id="gobby-test-types-suppressions",
            label="Gobby Python suppression ratchet",
            languages=["python"],
            bounded_inputs=True,
            categories=["type_check"],
            prefixes=["gobby test-types suppressions"],
            required_args_all=["--baseline"],
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
            bounded_inputs=True,
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
            "js-ts-script-checks",
            "JavaScript and TypeScript package script checks",
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
            ],
            forbidden_args_any=_MUTATING_VALIDATION_ARGS,
        ),
        _matcher(
            "js-ts-direct-checks",
            "JavaScript and TypeScript lint/type checks",
            ["javascript", "typescript"],
            ["lint", "type_check", "format"],
            [
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
            bounded_inputs=True,
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
            bounded_inputs=True,
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
            ],
        ),
        _matcher(
            "java-kotlin-static-checks",
            "Java and Kotlin static checks",
            ["java", "kotlin"],
            ["lint"],
            ["ktlint"],
            bounded_inputs=True,
        ),
        _matcher(
            "php-validation",
            "PHP tests and checks",
            ["php"],
            ["test", "lint"],
            ["phpunit", "vendor/bin/phpunit", "composer test"],
        ),
        _matcher(
            "php-static-checks",
            "PHP static checks",
            ["php"],
            ["lint", "type_check"],
            ["phpstan", "psalm"],
            bounded_inputs=True,
        ),
        _matcher(
            "dart-validation",
            "Dart and Flutter tests and checks",
            ["dart"],
            ["test", "lint", "type_check"],
            ["dart test", "flutter test"],
        ),
        _matcher(
            "dart-static-checks",
            "Dart and Flutter static checks",
            ["dart"],
            ["lint", "type_check"],
            ["dart analyze", "flutter analyze"],
            bounded_inputs=True,
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
            bounded_inputs=True,
        ),
        _matcher(
            "c-cpp-validation",
            "C and C++ tests and checks",
            ["c", "cpp"],
            ["test", "lint", "type_check"],
            ["ctest", "cmake --build"],
        ),
        _matcher(
            "c-cpp-static-checks",
            "C and C++ static checks",
            # gcode classifies `.h` headers by content, so a header can index as objc.
            ["c", "cpp", "objc"],
            ["lint"],
            ["clang-tidy", "cppcheck"],
            bounded_inputs=True,
        ),
        _matcher(
            "elixir-validation",
            "Elixir tests and checks",
            ["elixir"],
            ["test", "lint"],
            ["mix test"],
        ),
        # mix.exs aliases can redefine any mix task to run shell commands, so mix stays unbounded.
        _matcher(
            "elixir-static-checks",
            "Elixir static checks",
            ["elixir"],
            ["lint"],
            ["mix credo"],
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
            ["rspec", "bundle exec rspec", "rake test"],
        ),
        _matcher(
            "ruby-static-checks",
            "Ruby static checks",
            ["ruby"],
            ["lint"],
            ["rubocop"],
            bounded_inputs=True,
        ),
        _matcher(
            "swift-validation",
            "Swift tests and checks",
            ["swift"],
            ["test", "lint"],
            ["swift test"],
        ),
        _matcher(
            "swift-static-checks",
            "Swift static checks",
            ["swift"],
            ["lint"],
            ["swiftlint"],
            bounded_inputs=True,
        ),
        # Unbounded: markdownlint loads JavaScript rules and configs.
        _matcher(
            "data-doc-validation",
            "Markdown YAML and JSON checks",
            ["markdown", "yaml", "json"],
            ["lint", "format"],
            ["markdownlint", "yamllint", "jsonlint"],
        ),
        _matcher(
            "actionlint",
            "GitHub Actions workflow lint",
            ["yaml"],
            ["lint"],
            ["actionlint"],
            forbidden_args_any=["-help", "-version", "-init-config", "--init-config"],
            bounded_inputs=True,
        ),
        # Unbounded: jq --raw-input and --rawfile read any text file.
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
    bounded_inputs: bool = False,
) -> ValidationCommandMatcher:
    if not required_args_any_for:
        return ValidationCommandMatcher(
            id=matcher_id,
            label=label,
            languages=languages,
            bounded_inputs=bounded_inputs,
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
        bounded_inputs=bounded_inputs,
        categories=categories,
        prefixes=expanded_prefixes,
        required_args_any=required_args_any or [],
        forbidden_args_any=forbidden_args_any or [],
        non_executing_args_any=_NON_EXECUTING_VALIDATION_ARGS,
        evidence_weakening_args_any=evidence_weakening_args_any or [],
        evidence_weakening_bare_args_after=evidence_weakening_bare_args_after or [],
    )
