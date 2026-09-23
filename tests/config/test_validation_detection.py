from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.shell_lexing import shell_command_segments
from gobby.config.validation_detection import (
    ValidationCommandWrapper,
    ValidationDetectionConfig,
    classify_validation_command,
    classify_validation_segments,
    is_validation_command,
    load_project_validation_detection,
    resolve_validation_detection_config,
    save_project_validation_detection,
)
from gobby.config.validation_matchers import ValidationCommandMatcher, builtin_validation_matchers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command,matcher_id",
    [
        ("GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_hooks.py -v", "python-tests"),
        ("python -m coverage run -m pytest tests/foo.py", "python-tests"),
        ("python -m flake8 src tests", "python-lint-type"),
        ("npm run test -- --watch=false", "js-ts-tests"),
        ("npx playwright test tests/terminal-colors.spec.ts --workers=1", "js-ts-tests"),
        ("deno lint", "js-ts-direct-checks"),
        ("pnpm run lint", "js-ts-script-checks"),
        ("cargo check --no-default-features", "rust-validation"),
        ("cargo nextest run", "rust-validation"),
        ("cargo clippy --no-default-features -- -D warnings", "rust-validation"),
        ("cargo fmt --all -- --check", "rust-format-check"),
        ("ruff format --check src tests", "python-format-check"),
        ("uv run ruff format --check src/", "python-format-check"),
        ("uv run ruff check src/", "python-lint-type"),
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
        ("jq empty .gobby/project.json", "json-jq-validation"),
        ("jq -e '.verification' .gobby/project.json", "json-jq-validation"),
        ("jq --exit-status '.verification' .gobby/project.json", "json-jq-validation"),
        ("git diff --check", "git-diff-check"),
        ("git diff HEAD~2..HEAD --check", "git-diff-check"),
        ("git diff --check origin/main...HEAD -- src tests", "git-diff-check"),
        ("actionlint .github/workflows/rust-ci.yml", "actionlint"),
    ],
)
def test_builtin_validation_detection_accepts_common_commands(
    command: str,
    matcher_id: str,
) -> None:
    match = classify_validation_command(command)
    assert match is not None
    assert match.matcher_id == matcher_id


def test_test_types_ratchet_requires_baseline_and_fail_on_new() -> None:
    match = classify_validation_command(
        "uv run gobby test-types audit tests/ --baseline baseline.json --fail-on-new"
    )

    assert match is not None
    assert match.matcher_id == "gobby-test-types-audit"
    assert match.categories == ("type_check",)
    assert classify_validation_command("gobby test-types audit tests/") is None
    assert (
        classify_validation_command("gobby test-types audit tests/ --baseline baseline.json")
        is None
    )
    assert classify_validation_command("gobby test-types audit tests/ --fail-on-new") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby test-types audit tests/",
        "uv run gobby test-types audit tests/ --baseline baseline.json",
        "uv run gobby test-types audit tests/ --fail-on-new",
    ],
)
def test_test_types_ratchet_rejects_wrapped_commands_missing_required_flags(
    command: str,
) -> None:
    assert classify_validation_command(command) is None


def test_test_types_suppression_ratchet_requires_baseline() -> None:
    commands = [
        "gobby test-types suppressions . --baseline suppressions.json",
        "uv run gobby test-types suppressions . --baseline suppressions.json",
    ]

    matches = [classify_validation_command(command) for command in commands]

    assert all(match is not None for match in matches)
    assert [match.matcher_id for match in matches if match is not None] == [
        "gobby-test-types-suppressions",
        "gobby-test-types-suppressions",
    ]
    assert [match.categories for match in matches if match is not None] == [
        ("type_check",),
        ("type_check",),
    ]
    assert classify_validation_command("gobby test-types suppressions .") is None
    assert (
        classify_validation_command(
            "gobby test-types suppressions . --baseline suppressions.json --help"
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git diff --stat",
        "git diff --check --output=whitespace.txt",
        "git diff --check --ext-diff",
        "git diff --check --textconv",
        "git add --all",
        "cargo build",
        "npm install",
        "prettier . --write",
        "ruff format src tests",
        "uv run ruff format src/",
        "ruff check --fix src",
        "eslint . --fix",
        "dotnet format",
        "rust-token-killer -- 'git status'",
        "python script.py",
        "pytest --collect-only",
        "pytest --co",
        "pytest --version",
        "pytest --help",
        "pytest --fixtures",
        "pytest --markers",
        "ruff check --help",
        "mypy --install-types",
        "jq '.verification' .gobby/project.json",
        "actionlint -help",
        "actionlint --help",
        "actionlint -version",
        "actionlint --version",
        "actionlint -init-config",
        "actionlint --init-config",
    ],
)
def test_builtin_validation_detection_rejects_non_validation_commands(command: str) -> None:
    assert is_validation_command(command) is False


@pytest.mark.parametrize("command", ["actionlint", "actionlint .github/workflows/ci.yml"])
def test_actionlint_records_lint_category(command: str) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.categories == ("lint",)
    assert match.languages == ("yaml",)
    assert match.normalized_command == command
    assert not match.evidence_requires_confirmation


@pytest.mark.parametrize(
    "options",
    [
        "--directory /tmp/worktree",
        "--project /tmp/worktree",
        "--with pytest-cov",
        "-w pytest-cov",
        "--with-requirements dev.txt",
        "--with-editable .",
        "--extra dev",
        "--group test",
        "--package gobby",
        "--env-file .env",
        "--python 3.13",
        "-p 3.13",
        "--cache-dir /tmp/cache",
        "--color never",
        "--config-setting editable_mode=compat",
        "-C editable_mode=compat",
        "--directory /tmp/worktree --with pytest-cov",
    ],
)
def test_uv_run_options_with_values_are_stripped_before_the_runner(options: str) -> None:
    # Each of these consumes its value; an unlisted one is read as the command
    # and a real test run is silently never credited.
    match = classify_validation_command(
        f"GOBBY_TEST_PROTECT=1 uv run {options} pytest tests/x.py -q"
    )

    assert match is not None
    assert match.matcher_id == "python-tests"
    assert match.categories == ("test",)
    assert match.normalized_argv == ("pytest", "tests/x.py", "-q")


@pytest.mark.parametrize("flag", ["--no-install", "--no", "--yes", "-y"])
def test_npx_valueless_flags_do_not_block_detection(flag: str) -> None:
    match = classify_validation_command(f"npx {flag} vitest run src/hooks")

    assert match is not None
    assert match.matcher_id == "js-ts-tests"
    assert match.categories == ("test",)
    assert match.normalized_argv == ("vitest", "run", "src/hooks")
    assert match.wrapper_chain == ("npx",)


@pytest.mark.parametrize("options", ["-p vitest", "--package vitest", "--workspace web -y"])
def test_npx_options_with_values_are_stripped_before_the_runner(options: str) -> None:
    # A value-taking option stripped alone leaves its value where the runner should
    # be, so the real test run is misread or never credited.
    match = classify_validation_command(f"npx {options} vitest run src/hooks")

    assert match is not None
    assert match.matcher_id == "js-ts-tests"
    assert match.categories == ("test",)
    assert match.normalized_argv == ("vitest", "run", "src/hooks")
    assert match.wrapper_chain == ("npx",)


def test_path_qualified_local_binary_is_detected() -> None:
    match = classify_validation_command("./node_modules/.bin/vitest run src/hooks")

    assert match is not None
    assert match.matcher_id == "js-ts-tests"
    assert match.categories == ("test",)
    assert match.normalized_argv == ("./node_modules/.bin/vitest", "run", "src/hooks")


def test_path_qualified_non_validation_binary_is_not_detected() -> None:
    assert classify_validation_command("./tools/deploy.sh") is None
    assert classify_validation_command("./tools/deploy.sh run src/hooks") is None


def test_default_wrapper_rules_apply_to_explicit_config() -> None:
    match = classify_validation_command(
        "rust-token-killer -- 'cargo check'", ValidationDetectionConfig()
    )

    assert match is not None
    assert match.matcher_id == "rust-validation"
    assert match.wrapper_chain == ("rust-token-killer-command-string",)


@pytest.mark.parametrize(
    "command,segments",
    [
        ("pytest; true", [["pytest"], ["true"]]),
        ("pytest>/dev/null", [["pytest"]]),
        ("pytest 2>/dev/null", [["pytest"]]),
        ("pytest -k 'value<3'", [["pytest", "-k", "value<3"]]),
        ("pytest |& tee pytest.log", [["pytest"], ["tee", "pytest.log"]]),
    ],
)
def test_shell_command_segments_handle_glued_operators_and_redirections(
    command: str,
    segments: list[list[str]],
) -> None:
    assert shell_command_segments(command) == segments


@pytest.mark.parametrize(
    "command,segments",
    [
        ("cat > notes.md <<'EOF'\nuv run pytest tests/\nEOF", [["cat"]]),
        ("cat > notes.md <<-EOF\n\tuv run pytest tests/\n\tEOF", [["cat"]]),
        ("cat <<'EOF-2'\nuv run pytest tests/\nEOF-2\ntrue", [["cat"], ["true"]]),
        ('cat <<"END.SQL"\nuv run pytest tests/\nEND.SQL\ntrue', [["cat"], ["true"]]),
        ("cat <<END-MARKER\nuv run pytest tests/\nEND-MARKER\ntrue", [["cat"], ["true"]]),
        ("cat <<'END MARK;1'\nuv run pytest tests/\nEND MARK;1\ntrue", [["cat"], ["true"]]),
        ("cat <<E'ND-'2\nuv run pytest tests/\nEND-2\ntrue", [["cat"], ["true"]]),
        ("cat <<END\\ MARK\nuv run pytest tests/\nEND MARK\ntrue", [["cat"], ["true"]]),
        (
            "cat <<-END/MARK+1\n\tuv run pytest tests/\n\tEND/MARK+1\ntrue",
            [["cat"], ["true"]],
        ),
        (
            "cat <<-'END;MARK'\n\tuv run pytest tests/\n\tEND;MARK\ntrue",
            [["cat"], ["true"]],
        ),
        (
            'cat > a <<"ONE" > b <<TWO\npytest a\nONE\npytest b\nTWO\ntrue',
            [["cat"], ["true"]],
        ),
        # `<<<` is a herestring with no body, so the command still parses.
        ("pytest <<<'inline input'", [["pytest"]]),
    ],
)
def test_shell_command_segments_drop_heredoc_bodies(
    command: str,
    segments: list[list[str]],
) -> None:
    # A heredoc body is data, not a command sequence. Newline is a segment
    # separator, so an unstripped body would let a merely quoted test runner be
    # credited as a real validation run.
    assert shell_command_segments(command) == segments


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.md <<'EOF'\nuv run pytest tests/\nEOF",
        "cat > notes.md <<-EOF\n\tGOBBY_TEST_PROTECT=1 uv run pytest tests/\n\tEOF",
        "cat <<'EOF-2'\nuv run pytest tests/\nEOF-2",
        "cat <<-END.SQL\n\tGOBBY_TEST_PROTECT=1 uv run pytest tests/\n\tEND.SQL",
    ],
)
def test_heredoc_bodies_are_not_validation_commands(command: str) -> None:
    assert classify_validation_command(command) is None


def test_newline_separated_command_after_directory_change_still_matches() -> None:
    match = classify_validation_command(
        "cd /tmp/repo\nGOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q"
    )

    assert match is not None
    assert match.categories == ("test",)


@pytest.mark.parametrize(
    "command,segment_index,operators",
    [
        ("pytest || echo ok", 0, ("||",)),
        ("true; pytest", 1, (";",)),
        ("bash -lc 'pytest || echo ok'", 0, ("||",)),
        ("bash -lc 'echo ok; pytest'", 1, (";",)),
        ("pytest |& tee pytest.log", 0, ("|&",)),
    ],
)
def test_compound_validation_match_reports_segment_metadata(
    command: str,
    segment_index: int,
    operators: tuple[str, ...],
) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.segment_index == segment_index
    assert match.segment_count == 2
    assert match.shell_operators == operators
    assert match.is_compound is True


@pytest.mark.parametrize(
    "command",
    [
        "pytest -k nonexistent",
        "go test ./... -run nonexistent",
        "cargo test nonexistent",
        "cargo nextest run nonexistent",
    ],
)
def test_selector_narrowed_validation_requires_execution_confirmation(command: str) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.evidence_requires_confirmation is True


@pytest.mark.parametrize(
    "command,requires_confirmation",
    [
        ("python -m pytest tests/config", False),
        ("python3 -m pytest tests/config", False),
        ("python -m pytest -m slow tests/config", True),
        ("pytest -m slow tests/config", True),
    ],
)
def test_python_module_flag_is_not_mistaken_for_pytest_marker_selection(
    command: str,
    requires_confirmation: bool,
) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.evidence_requires_confirmation is requires_confirmation


@pytest.mark.parametrize(
    "command,normalized_argv,wrapper_chain",
    [
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
        (
            "GOBBY_TEST_PROTECT=1 uv run rtk pytest tests/config",
            ("pytest", "tests/config"),
            ("uv-run", "rtk"),
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


def test_nice_without_delimiter_is_detected() -> None:
    match = classify_validation_command("nice -n 15 cargo clippy -p gobby-code")
    assert match is not None
    assert match.normalized_argv == ("cargo", "clippy", "-p", "gobby-code")
    assert match.wrapper_chain == ("nice",)


def test_nice_with_delimiter_is_detected() -> None:
    match = classify_validation_command("nice -n 15 -- cargo clippy -p gobby-code")
    assert match is not None
    assert match.normalized_argv == ("cargo", "clippy", "-p", "gobby-code")
    assert match.wrapper_chain == ("nice",)


def test_nice_numeric_option_and_absolute_path_are_detected() -> None:
    match = classify_validation_command("/usr/bin/nice -5 -- cargo clippy -p gobby-code")
    assert match is not None
    assert match.normalized_argv == ("cargo", "clippy", "-p", "gobby-code")
    assert match.wrapper_chain == ("nice",)


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


def test_project_validation_detection_round_trip(tmp_path: Path) -> None:
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


def test_bounded_inputs_requires_languages() -> None:
    with pytest.raises(ValidationError, match="bounded_inputs requires languages"):
        ValidationCommandMatcher(id="lint", label="Lint", prefixes=["lint"], bounded_inputs=True)


@pytest.mark.parametrize(
    ("command", "bounded"),
    [
        (
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            True,
        ),
        ("npx eslint web/src", True),
        ("tsc --noEmit", True),
        ("uv run mypy src/", True),
        ("uv run ruff check src/", True),
        ("phpstan analyse src", True),
        ("npm run lint", False),
        ("tox -e lint", False),
        ("go vet ./...", False),
        ("cargo clippy -- -D warnings", False),
        ("uv run pytest tests/config", False),
        ("npx vitest run", False),
        ("prettier --check web", False),
        ("jq -e -R 'test(\"TODO\") | not' src/app.py", False),
        ("markdownlint --rules ./custom.js docs", False),
        ("mix credo --strict", False),
    ],
)
def test_builtin_matchers_bound_only_direct_checkers(command: str, bounded: bool) -> None:
    match = classify_validation_command(command)

    assert match is not None
    assert match.bounded_inputs is bounded


def test_bounded_builtin_matchers_carry_no_test_category() -> None:
    bounded = [matcher for matcher in builtin_validation_matchers() if matcher.bounded_inputs]

    assert {"php-static-checks", "ruby-static-checks"} <= {matcher.id for matcher in bounded}
    assert [matcher.id for matcher in bounded if "test" in matcher.categories] == []


def test_classify_validation_segments_keeps_every_validation_segment() -> None:
    command = (
        'git stash push -m "tmp" src/gobby/servers/auth.py -q\n'
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_auth.py::test_case -q\n"
        "git stash pop -q\n"
        "uv run ruff check src/gobby/servers/auth.py"
    )

    segments = classify_validation_segments(command)

    assert [match.normalized_command for match in segments] == [
        "pytest tests/servers/test_auth.py::test_case -q",
        "ruff check src/gobby/servers/auth.py",
    ]
    assert [match.segment_index for match in segments] == [1, 3]
    first = classify_validation_command(command)
    assert first is not None
    assert first == segments[0]
    assert classify_validation_segments("git status --short") == ()
