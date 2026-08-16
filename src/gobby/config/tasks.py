"""
Task management configuration module.

Contains task-related Pydantic config models:
- CompactHandoffConfig: Compact handoff context configuration
- PatternCriteriaConfig: Pattern-specific validation criteria templates
- TaskExpansionConfig: Task breakdown/expansion settings
- TaskValidationConfig: Task completion validation settings
- GobbyTasksConfig: Combined gobby-tasks MCP server config
- WorkflowConfig: Workflow engine configuration
- WorkflowVariablesConfig: Default values for session workflow variables

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig, FeatureProfile
from gobby.config.url_validation import validate_optional_endpoint_url

DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 90.0

__all__ = [
    "CompactHandoffConfig",
    "FileExtractionConfig",
    "PatternCriteriaConfig",
    "TaskExpansionConfig",
    "TaskValidationConfig",
    "GobbyTasksConfig",
    "WorkflowConfig",
    "WorkflowVariablesConfig",
    "merge_workflow_variables",
]


class CompactHandoffConfig(BaseModel):
    """Compact handoff context configuration for /compact command."""

    enabled: bool = Field(
        default=True,
        description="Enable compact handoff context extraction and injection",
    )
    refresh_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Maximum seconds compact_self waits for pre-compact handoff summary refresh "
            "before falling back to the latest digest."
        ),
    )


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
    """Configuration for task validation (checking completion against criteria)."""

    profile: FeatureProfile = Field(
        default=FeatureProfile.MID,
        description="Capability profile for task validation.",
    )
    enabled: bool = Field(
        default=True,
        description="Enable automated task validation",
    )
    system_prompt: str = Field(
        default="You are a QA validator. Output ONLY valid JSON. No markdown, no explanation, no code blocks. Just the raw JSON object.",
        description="System prompt for task validation",
    )
    prompt_path: str | None = Field(
        default=None,
        description="Path to custom validation prompt template (e.g., 'validation/validate')",
    )
    criteria_prompt_path: str | None = Field(
        default=None,
        description="Path to custom criteria generation prompt template (e.g., 'validation/criteria')",
    )
    criteria_system_prompt: str = Field(
        default="You are a QA engineer writing acceptance criteria. CRITICAL: Only include requirements explicitly stated in the task. Do NOT invent specific values, thresholds, timeouts, or edge cases that aren't mentioned. Vague tasks get vague criteria. Use markdown checkboxes.",
        description="System prompt for generating validation criteria",
    )

    max_iterations: int = Field(
        default=10,
        description="Maximum validation attempts before escalation",
    )
    close_validation_escalation_threshold: int = Field(
        default=5,
        description="Invalid or pending close verdicts before atomic task escalation.",
    )
    close_review_prompt_max_chars: int = Field(
        default=32_000,
        description="Maximum rendered character count for the task-close criteria-review prompt.",
    )
    # Escalation settings
    escalation_enabled: bool = Field(
        default=True,
        description="Enable task escalation on repeated validation failures",
    )
    escalation_notify: Literal["webhook", "slack", "none"] = Field(
        default="none",
        description="Notification method when task is escalated",
    )
    escalation_webhook_url: str | None = Field(
        default=None,
        description="Webhook URL for escalation notifications",
    )
    # Auto-generation settings
    auto_generate_on_create: bool = Field(
        default=True,
        description="Auto-generate validation criteria when creating tasks via create_task",
    )
    auto_generate_on_expand: bool = Field(
        default=True,
        description="Auto-generate validation criteria when expanding tasks via expand_task",
    )

    @field_validator(
        "max_iterations",
        "close_validation_escalation_threshold",
        "close_review_prompt_max_chars",
    )
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    @field_validator("escalation_webhook_url")
    @classmethod
    def validate_escalation_webhook_url(cls, value: str | None) -> str | None:
        return validate_optional_endpoint_url(value, field_name="escalation_webhook_url")


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
