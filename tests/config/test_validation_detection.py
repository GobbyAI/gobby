from __future__ import annotations

import json
import shlex

import pytest

from gobby.config.validation_detection import (
    ValidationCommandMatcher,
    ValidationCommandWrapper,
    ValidationDetectionConfig,
    classify_validation_command,
    is_validation_command,
    load_project_validation_detection,
    resolve_validation_detection_config,
    save_project_validation_detection,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command,matcher_id",
    [
        ("GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_hooks.py -v", "python-tests"),
        ("python -m coverage run -m pytest tests/foo.py", "python-tests"),
        ("python -m flake8 src tests", "python-lint-type-format"),
        ("npm run test -- --watch=false", "js-ts-tests"),
        ("deno lint", "js-ts-lint-type-format"),
        ("pnpm run lint", "js-ts-lint-type-format"),
        ("cargo check --no-default-features", "rust-validation"),
        ("cargo nextest run", "rust-validation"),
        ("cargo clippy --no-default-features -- -D warnings", "rust-validation"),
        ("cargo fmt --all -- --check", "rust-format-check"),
        ("ruff format --check src tests", "python-format-check"),
        ("/Users/josh/.gobby/bin/gsqz -- 'uv run ruff check src/'", "python-lint-type-format"),
        ("/Users/josh/.gobby/bin/gsqz -- 'cargo check'", "rust-validation"),
        ("gsqz -- 'cargo check --no-default-features'", "rust-validation"),
        ("rust-token-killer -- cargo check", "rust-validation"),
        ("rust-token-killer -- 'cargo check --no-default-features'", "rust-validation"),
        ("timeout 30 -- npm test", "js-ts-tests"),
        ("bash -lc 'GOBBY_TEST_PROTECT=1 uv run pytest tests/config'", "python-tests"),
        ("env RUSTFLAGS=-Awarnings -- cargo check", "rust-validation"),
        ("go test ./...", "go-validation"),
        ("dotnet format --verify-no-changes", "csharp-format-check"),
        ("mix format --check-formatted", "elixir-format-check"),
        ("prettier . --check", "js-ts-format-check"),
        ("swift test", "swift-validation"),
    ],
)
def test_builtin_validation_detection_accepts_common_commands(
    command: str,
    matcher_id: str,
) -> None:
    match = classify_validation_command(command)
    assert match is not None
    assert match.matcher_id == matcher_id


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "cargo build",
        "npm install",
        "prettier . --write",
        "ruff format src tests",
        "ruff check --fix src",
        "eslint . --fix",
        "dotnet format",
        "gsqz -- 'git status'",
        "rust-token-killer -- 'git status'",
        "python script.py",
    ],
)
def test_builtin_validation_detection_rejects_non_validation_commands(command: str) -> None:
    assert is_validation_command(command) is False


def test_default_wrapper_rules_apply_to_explicit_config() -> None:
    match = classify_validation_command("gsqz -- 'cargo check'", ValidationDetectionConfig())

    assert match is not None
    assert match.matcher_id == "rust-validation"
    assert match.wrapper_chain == ("gsqz-command-string",)


@pytest.mark.parametrize(
    "command,normalized_argv,wrapper_chain",
    [
        (
            "gsqz -- 'uv run ruff check src/'",
            ("ruff", "check", "src/"),
            ("gsqz-command-string", "uv-run"),
        ),
        (
            "/Users/josh/.gobby/bin/gsqz -- 'cargo check'",
            ("cargo", "check"),
            ("gsqz-command-string",),
        ),
        (
            "rust-token-killer -- cargo check",
            ("cargo", "check"),
            ("rust-token-killer-command-string",),
        ),
        (
            "rust-token-killer -- 'cargo check --no-default-features'",
            ("cargo", "check", "--no-default-features"),
            ("rust-token-killer-command-string",),
        ),
        ("timeout 30 -- npm test", ("npm", "test"), ("timeout",)),
        (
            "bash -lc 'GOBBY_TEST_PROTECT=1 uv run pytest tests/config'",
            ("pytest", "tests/config"),
            ("bash-lc", "uv-run"),
        ),
        ("env RUSTFLAGS=-Awarnings -- cargo check", ("cargo", "check"), ("env",)),
    ],
)
def test_wrapped_validation_commands_record_normalized_metadata(
    command: str,
    normalized_argv: tuple[str, ...],
    wrapper_chain: tuple[str, ...],
) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.command == command
    assert match.normalized_argv == normalized_argv
    assert match.normalized_command == shlex.join(normalized_argv)
    assert match.wrapper_chain == wrapper_chain


def test_disabled_builtin_matcher_is_not_used() -> None:
    config = ValidationDetectionConfig(
        disabled_builtin_matcher_ids=["rust-validation"],
    )
    assert classify_validation_command("cargo check", config) is None


def test_custom_matcher_extends_detection() -> None:
    config = ValidationDetectionConfig(
        builtin_matchers_enabled=False,
        custom_matchers=[
            ValidationCommandMatcher(
                id="project-ci",
                label="Project CI",
                categories=["test"],
                prefixes=["./scripts/ci"],
            )
        ],
    )

    match = classify_validation_command("./scripts/ci --fast", config)

    assert match is not None
    assert match.matcher_id == "project-ci"


def test_custom_wrapper_rule_extends_detection() -> None:
    config = ValidationDetectionConfig(
        builtin_matchers_enabled=False,
        wrapper_rules=[
            ValidationCommandWrapper(
                id="project-wrapper",
                label="Project wrapper",
                kind="command_string",
                prefixes=["project-wrapper --"],
            )
        ],
        custom_matchers=[
            ValidationCommandMatcher(
                id="project-ci",
                label="Project CI",
                categories=["test"],
                prefixes=["./scripts/ci"],
            )
        ],
    )

    match = classify_validation_command("project-wrapper -- './scripts/ci --fast'", config)

    assert match is not None
    assert match.matcher_id == "project-ci"
    assert match.wrapper_chain == ("project-wrapper",)


def test_project_validation_detection_round_trip(tmp_path) -> None:
    project_file = tmp_path / ".gobby" / "project.json"
    project_file.parent.mkdir()
    project_file.write_text(json.dumps({"name": "demo"}), encoding="utf-8")

    saved = save_project_validation_detection(
        str(tmp_path),
        {
            "builtin_matchers_enabled": False,
            "custom_matchers": [
                {
                    "id": "demo-test",
                    "label": "Demo test",
                    "prefixes": ["demo test"],
                }
            ],
        },
    )

    assert saved is not None
    loaded = load_project_validation_detection(str(tmp_path))
    assert loaded is not None
    assert loaded["builtin_matchers_enabled"] is False
    resolved = resolve_validation_detection_config(project_path=str(tmp_path))
    assert classify_validation_command("demo test", resolved) is not None
