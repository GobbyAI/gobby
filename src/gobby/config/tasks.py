"""
Task management configuration module.

Contains task-related Pydantic config models:
- PatternCriteriaConfig: Pattern-specific validation criteria templates
- TaskExpansionConfig: Task breakdown/expansion settings
- TaskValidationConfig: Task completion validation settings
- TaskClosePolicyConfig: Project step records required before task close
- GobbyTasksConfig: Combined gobby-tasks MCP server config
- WorkflowConfig: Workflow engine configuration
- WorkflowVariablesConfig: Default values for session workflow variables

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig, FeatureProfile

# Sits above the shared blocking-effect budget plus the RTK proxy stage's
# reservation, and below the adapter timeout, which in turn sits inside the
# ghook transport window. ``DaemonConfig.validate_hook_timeout_order`` enforces
# the whole ladder.
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 24.0

__all__ = [
    "FileExtractionConfig",
    "PatternCriteriaConfig",
    "TaskExpansionConfig",
    "TaskValidationConfig",
    "TaskClosePolicyConfig",
    "load_project_task_close_policy",
    "GobbyTasksConfig",
    "WorkflowConfig",
    "WorkflowVariablesConfig",
    "merge_workflow_variables",
]


class PatternCriteriaConfig(BaseModel):
    """Configuration for pattern-specific validation criteria templates.

    Defines validation criteria templates for common development patterns like
    strangler-fig, TDD, and refactoring. Templates can use placeholders that
    get replaced with actual values from project verification config.

    Placeholders:
    - {unit_tests}: Unit test command from project verification
    - {type_check}: Type check command from project verification
    - {lint}: Lint command from project verification
    - {original_module}, {new_module}, {function}, {original_file}: For strangler-fig pattern
    """

    patterns: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "strangler-fig": [
                "Original import still works: `from {original_module} import {function}`",
                "New import works: `from {new_module} import {function}`",
                "Delegation exists: `grep -c 'from .{new_module} import' {original_file}` >= 1",
                "No circular imports: `python -c 'from {original_module} import *'`",
            ],
            "tdd": [
                "Tests written before implementation (verify git log order)",
                "Tests initially fail (red phase)",
                "Implementation makes tests pass (green phase)",
            ],
            "refactoring": [
                "All existing tests pass: `{unit_tests}`",
                "No new type errors: `{type_check}`",
                "No lint violations: `{lint}`",
            ],
        },
        description="Pattern name to list of validation criteria templates. "
        "Templates can use placeholders like {unit_tests}, {type_check}, {lint}.",
    )
    detection_keywords: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "strangler-fig": [
                "strangler fig",
                "strangler-fig",
                "strangler pattern",
                "delegation pattern",
            ],
            "tdd": ["tdd", "test-driven", "test driven", "red-green", "red green"],
            "refactoring": ["refactor", "refactoring", "restructure", "reorganize"],
        },
        description="Pattern name to list of keywords that trigger pattern detection in task descriptions.",
    )


class TaskExpansionConfig(FeatureDefaultConfig):
    """Configuration for task expansion (breaking down broad tasks/epics)."""

    profile: FeatureProfile = Field(
        default=FeatureProfile.HIGH,
        description="Capability profile for task expansion.",
    )
    enabled: bool = Field(
        default=True,
        description="Enable automated task expansion",
    )

    prompt_path: str | None = Field(
        default=None,
        description="Path to custom user prompt template (e.g., 'expansion/user')",
    )
    system_prompt_path: str | None = Field(
        default=None,
        description="Path to custom system prompt template (e.g., 'expansion/system')",
    )
    default_strategy: Literal["auto", "phased", "sequential", "parallel"] = Field(
        default="auto",
        description="Default expansion strategy: auto (LLM decides), phased, sequential, or parallel",
    )
    timeout: float = Field(
        default=300.0,
        gt=0,
        description="Maximum time in seconds for entire task expansion (default: 5 minutes)",
    )
    pattern_criteria: PatternCriteriaConfig = Field(
        default_factory=PatternCriteriaConfig,
        description="Pattern-specific validation criteria templates",
    )


class TaskValidationConfig(FeatureDefaultConfig):
    """Model settings used by found-work confirmation."""

    profile: FeatureProfile = Field(
        default=FeatureProfile.MID,
        description="Capability profile for found-work confirmation.",
    )
    enabled: bool = Field(default=True, description="Enable found-work confirmation")
    confirmation_timeout_seconds: float = Field(
        default=8.0,
        gt=0,
        description="Wall-clock bound for found-work confirmation, including provider fallbacks.",
    )


TASK_CLOSE_POLICY_KEY = "task_close_policy"


class TaskClosePolicyConfig(BaseModel):
    """Approved step records a project requires before its tasks close.

    Epics never need records: their children carry them.
    """

    model_config = ConfigDict(extra="forbid")

    enforce_step_records: bool = Field(
        default=False,
        description="Require approved implementation and closing records for non-epic tasks.",
    )
    test_categories: list[str] = Field(
        default_factory=list,
        description="Task categories that also need an approved test record.",
    )
    review_categories: list[str] = Field(
        default_factory=lambda: ["code", "docs", "research"],
        description="Task categories that also need an approved independent review record.",
    )

    @field_validator("test_categories", "review_categories")
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        from gobby.storage.tasks._models import VALID_CATEGORIES

        unknown = sorted(set(value) - VALID_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown task category: {', '.join(unknown)}")
        return value


def load_project_task_close_policy(project_path: str) -> TaskClosePolicyConfig:
    """Read a checkout's `.gobby/project.json` policy; an invalid policy fails closed."""
    marker = Path(project_path) / ".gobby" / "project.json"
    try:
        text = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return TaskClosePolicyConfig()
    try:
        return TaskClosePolicyConfig.model_validate(json.loads(text).get(TASK_CLOSE_POLICY_KEY, {}))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid {TASK_CLOSE_POLICY_KEY} in {marker}: {exc}") from exc


class FileExtractionConfig(BaseModel):
    """Configuration for extracting file paths from task descriptions.

    Used by extract_mentioned_files() to identify task-relevant files
    for validation context prioritization.
    """

    # Comprehensive list of file extensions to recognize
    file_extensions: list[str] = Field(
        default_factory=lambda: [
            # Python
            ".py",
            ".pyi",
            ".pyx",
            ".pxd",
            # JavaScript/TypeScript
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".mjs",
            ".cjs",
            # Web
            ".html",
            ".htm",
            ".css",
            ".scss",
            ".sass",
            ".less",
            ".vue",
            ".svelte",
            ".astro",
            # Data/Config
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".env",
            ".xml",
            ".csv",
            ".tsv",
            # Documentation
            ".md",
            ".markdown",
            ".rst",
            ".txt",
            ".adoc",
            # Shell/Scripts
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".ps1",
            ".bat",
            ".cmd",
            # Go
            ".go",
            ".mod",
            ".sum",
            # Rust
            ".rs",
            ".toml",
            # Java/Kotlin/JVM
            ".java",
            ".kt",
            ".kts",
            ".scala",
            ".groovy",
            ".gradle",
            # C/C++
            ".c",
            ".h",
            ".cpp",
            ".hpp",
            ".cc",
            ".hh",
            ".cxx",
            ".hxx",
            # C#/.NET
            ".cs",
            ".csproj",
            ".sln",
            ".fs",
            ".fsx",
            # Swift/Objective-C
            ".swift",
            ".m",
            ".mm",
            # Ruby
            ".rb",
            ".rake",
            ".gemspec",
            # PHP
            ".php",
            ".phtml",
            # Perl
            ".pl",
            ".pm",
            # Lua
            ".lua",
            # R
            ".r",
            ".R",
            ".rmd",
            ".Rmd",
            # Julia
            ".jl",
            # Elixir/Erlang
            ".ex",
            ".exs",
            ".erl",
            ".hrl",
            # Haskell
            ".hs",
            ".lhs",
            # OCaml
            ".ml",
            ".mli",
            # SQL
            ".sql",
            ".psql",
            ".mysql",
            # GraphQL
            ".graphql",
            ".gql",
            # Protobuf/Thrift
            ".proto",
            ".thrift",
            # Docker/Container
            ".dockerfile",
            # Terraform/IaC
            ".tf",
            ".tfvars",
            ".hcl",
            # Kubernetes
            ".k8s",
            # Nix
            ".nix",
            # Prisma
            ".prisma",
            # Other
            ".lock",
            ".log",
            ".diff",
            ".patch",
        ],
        description="File extensions to recognize when extracting paths from task descriptions",
    )

    # Files without extensions that should be recognized
    known_files: list[str] = Field(
        default_factory=lambda: [
            "Makefile",
            "makefile",
            "GNUmakefile",
            "Dockerfile",
            "dockerfile",
            "Containerfile",
            "Jenkinsfile",
            "Vagrantfile",
            "Rakefile",
            "rakefile",
            "Gemfile",
            "Podfile",
            "Brewfile",
            "Procfile",
            "Taskfile",
            "Justfile",
            "justfile",
            "Earthfile",
            "Tiltfile",
            "BUILD",
            "WORKSPACE",
            "CMakeLists",
            "meson.build",
            "SConstruct",
            "SConscript",
            "CHANGELOG",
            "CHANGES",
            "HISTORY",
            "README",
            "INSTALL",
            "LICENSE",
            "COPYING",
            "AUTHORS",
            "CONTRIBUTORS",
            "MAINTAINERS",
            "CODEOWNERS",
            ".gitignore",
            ".gitattributes",
            ".gitmodules",
            ".dockerignore",
            ".editorconfig",
            ".eslintrc",
            ".prettierrc",
            ".stylelintrc",
            ".babelrc",
            ".nvmrc",
            ".node-version",
            ".python-version",
            ".ruby-version",
            "requirements.txt",
            "constraints.txt",
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Cargo.toml",
            "Cargo.lock",
            "go.mod",
            "go.sum",
            "composer.json",
            "composer.lock",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "poetry.lock",
            "Pipfile",
            "Pipfile.lock",
            "tsconfig.json",
            "jsconfig.json",
            "webpack.config.js",
            "vite.config.js",
            "rollup.config.js",
            "jest.config.js",
            "vitest.config.js",
            "playwright.config.js",
            ".env.local",
            ".env.development",
            ".env.production",
            ".env.test",
        ],
        description="Known filenames without extensions to recognize",
    )

    # Common path prefixes that indicate a file path
    path_prefixes: list[str] = Field(
        default_factory=lambda: [
            "src/",
            "lib/",
            "pkg/",
            "packages/",
            "test/",
            "tests/",
            "spec/",
            "specs/",
            "__tests__/",
            "app/",
            "apps/",
            "internal/",
            "cmd/",
            "bin/",
            "scripts/",
            "tools/",
            "utils/",
            "config/",
            "configs/",
            "conf/",
            "settings/",
            "docs/",
            "doc/",
            "documentation/",
            "assets/",
            "static/",
            "public/",
            "resources/",
            "components/",
            "pages/",
            "views/",
            "templates/",
            "models/",
            "controllers/",
            "services/",
            "handlers/",
            "api/",
            "routes/",
            "middleware/",
            "fixtures/",
            "mocks/",
            "stubs/",
            "fakes/",
            "migrations/",
            "seeds/",
            "schemas/",
            ".github/",
            ".circleci/",
            ".gitlab/",
            ".gobby/",
            ".vscode/",
            ".idea/",
        ],
        description="Common path prefixes that indicate a file path",
    )


class GobbyTasksConfig(BaseModel):
    """Configuration for gobby-tasks internal MCP server."""

    model_config = {"populate_by_name": True}

    enabled: bool = Field(
        default=True,
        description="Enable gobby-tasks internal MCP server",
    )
    show_result_on_create: bool = Field(
        default=False,
        description="Show full task result on create_task (False = minimal output with just id)",
    )
    file_extraction: FileExtractionConfig = Field(
        default_factory=FileExtractionConfig,
        description="Configuration for extracting file paths from task descriptions",
    )
    expansion: TaskExpansionConfig = Field(
        default_factory=lambda: TaskExpansionConfig(),
        description="Task expansion configuration",
    )
    validation: TaskValidationConfig = Field(
        default_factory=lambda: TaskValidationConfig(),
        description="Task validation configuration",
    )


class WorkflowConfig(BaseModel):
    """Workflow engine configuration."""

    definition_regex_max_length: int = Field(
        default=2048,
        gt=0,
        description="Maximum characters in a definition-supplied regex; enforced before registry writes",
    )

    enabled: bool = Field(
        default=True,
        description="Enable workflow engine",
    )
    timeout: float = Field(
        default=DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
        description="Finite workflow timeout in seconds; zero selects the default",
    )
    debug_echo_context: bool = Field(
        default=False,
        description="Debug: echo additionalContext to system_message for terminal visibility",
    )

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        """Normalize the bundled zero sentinel and reject invalid deadlines."""
        if not math.isfinite(v) or v < 0:
            raise ValueError("Timeout must be finite and non-negative")
        if v == 0:
            return DEFAULT_WORKFLOW_TIMEOUT_SECONDS
        return v


class WorkflowVariablesConfig(BaseModel):
    """Default values for session workflow variables.

    These defaults are used when workflow YAML files don't specify values.
    Variables can be overridden per-session via set_variable MCP tool.

    The precedence order is:
    1. Explicit parameter (highest priority)
    2. Workflow variable (set at runtime)
    3. Workflow YAML default
    4. This config (lowest priority)
    """

    require_task_before_edit: bool = Field(
        default=False,
        description="Require an active task (in_progress) before allowing file edits",
    )
    session_task: str | list[str] | None = Field(
        default=None,
        description="Task(s) to complete before stopping. "
        "Values: None (no enforcement), task ID, list of IDs, or '*' (all ready tasks)",
    )


def merge_workflow_variables(
    yaml_defaults: dict[str, Any],
    db_overrides: dict[str, Any] | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Merge workflow YAML defaults with DB session overrides.

    Implements the merge order: YAML defaults → DB overrides → effective config.
    DB overrides take precedence over YAML defaults.

    Args:
        yaml_defaults: Variable defaults from workflow YAML definition.
        db_overrides: Session-specific overrides from persisted session variables.
            Can be None if no session state exists.
        validate: If True, validate merged result through WorkflowVariablesConfig.
            Invalid values will raise ValidationError.

    Returns:
        Effective config dict with merged variables that actions can access.

    Raises:
        ValidationError: If validate=True and merged values fail validation.

    Example:
        >>> yaml_defaults = {"require_task_before_edit": False, "session_task": None}
        >>> db_overrides = {"require_task_before_edit": True}
        >>> effective = merge_workflow_variables(yaml_defaults, db_overrides)
        >>> effective["require_task_before_edit"]
        True
        >>> effective["session_task"]
        None
    """
    # Start with defaults
    effective = dict(yaml_defaults)

    # Apply DB overrides (takes precedence)
    if db_overrides:
        effective.update(db_overrides)

    # Validate through WorkflowVariablesConfig if requested
    if validate:
        # This will raise ValidationError for invalid values
        validated = WorkflowVariablesConfig(**effective)
        # Return as dict for action access
        return validated.model_dump()

    return effective
