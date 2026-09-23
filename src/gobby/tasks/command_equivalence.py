"""Conservative shell argument and target equivalence for validation evidence."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections import Counter

from gobby.config.shell_lexing import ParsedShellCommand
from gobby.config.validation_detection import normalize_validation_evidence_command

# Keep the original spelling until operators and redirections have been identified.
# shlex alone erases whether a token such as '|' was a literal argument.
_TOKEN = re.compile(
    r"""(?P<redirect>\d*(?:>>|>\||>&|>)|&>>?)"""
    r"""|(?P<operator>&&|\|\||\|&|[;&|\n])"""
    r"""|(?P<word>(?:[^\s;&|<>()'"\\]+|\\.|'[^']*'|"(?:\\.|[^"\\])*")+)"""
)


def parse_validation_shell(command: str) -> ParsedShellCommand:
    """Parse the supported exit-preserving shell subset, retaining quoted literals.

    Input redirects, substitutions and shell compound constructs are deliberately
    unsupported: they need execution context we do not have in a transcript.
    """
    segments: list[tuple[str, ...]] = []
    operators: list[str] = []
    current: list[str] = []
    cursor = 0
    redirect: str | None = None
    while cursor < len(command):
        if command[cursor] in " \t\r":
            cursor += 1
            continue
        match = _TOKEN.match(command, cursor)
        if match is None:
            return ParsedShellCommand((), ())
        cursor = match.end()
        raw = match.group()
        if match.lastgroup == "redirect":
            if redirect is not None:
                return ParsedShellCommand((), ())
            redirect = raw
        elif match.lastgroup == "operator":
            if redirect is not None or not current:
                return ParsedShellCommand((), ())
            segments.append(tuple(current))
            current = []
            operators.append(raw)
        else:
            # Reject expansion outside single quotes, including inside double quotes.
            if _has_expansion(raw):
                return ParsedShellCommand((), ())
            words = shlex.split(raw)
            if len(words) != 1:
                return ParsedShellCommand((), ())
            word = words[0]
            if redirect is not None:
                if redirect.endswith(">&") and word not in {"1", "2"}:
                    return ParsedShellCommand((), ())
                redirect = None
            else:
                current.append(word)
    if redirect is not None:
        return ParsedShellCommand((), ())
    if current:
        segments.append(tuple(current))
    return ParsedShellCommand(tuple(segments), tuple(operators))


def _has_expansion(word: str) -> bool:
    quote: str | None = None
    cursor = 0
    while cursor < len(word):
        char = word[cursor]
        if char == "\\" and quote != "'":
            cursor += 2
            continue
        if char == quote:
            quote = None
        elif char in {"'", '"'} and quote is None:
            quote = char
        elif char in {"$", "`"} and quote != "'":
            return True
        cursor += 1
    return False


def canonical_command(command: str) -> str | None:
    """Compare literal arguments and safe redirects without changing their meaning."""
    literal_free = re.sub(r'''\\.|'[^']*'|"(?:\\.|[^"\\])*"''', "", command)
    if any(char in literal_free for char in "*?[]{}~"):
        return None
    command = normalize_validation_evidence_command(command)
    parsed = parse_validation_shell(command)
    if not parsed.segments or len(parsed.segments) != len(parsed.operators) + 1:
        return None
    if any(operator != "&&" for operator in parsed.operators):
        return None
    return " && ".join(shlex.join(_cargo_arguments(list(part))) for part in parsed.segments)


def _cargo_arguments(tokens: list[str]) -> list[str]:
    start = 2 if tokens[:2] == ["uv", "run"] else 0
    if tokens[start : start + 1] != ["cargo"]:
        return tokens
    # cargo fmt accepts --check itself or forwards it to rustfmt after --.
    if tokens[start + 1 : start + 2] == ["fmt"] and tokens[-2:] == ["--", "--check"]:
        tokens = tokens[:-2] + ["--check"]
    result = tokens[: start + 2]
    forwarded = False
    for token in tokens[start + 2 :]:
        if token == "--":
            forwarded = True
        if not forwarded and token == "-p":
            result.append("--package")
        elif not forwarded and token.startswith("-p") and not token.startswith("--"):
            result.extend(("--package", token[2:]))
        elif not forwarded and token.startswith(("--package=", "--manifest-path=", "--target=")):
            result.extend(token.split("=", 1))
        elif forwarded and re.fullmatch(r"-[DAWF].+", token):
            result.extend((token[:2], token[2:]))
        else:
            result.append(token)
    return result


def target_covers(success: str, failure: str) -> bool:
    """A file/directory covers its descendants; a node id covers only itself."""
    if success == failure:
        return True
    if success.startswith("-") or failure.startswith("-") or "::" in success:
        return False
    failure_path = failure.split("::", 1)[0]
    if success == ".":
        return not failure_path.startswith(("/", "../")) and failure_path != ".."
    return failure_path == success or failure_path.startswith(success.rstrip("/") + "/")


def command_covers(executed: str, required: str) -> bool:
    """Credit scope-preserving command forms for explicitly allowlisted runners."""
    actual = canonical_command(executed)
    expected = canonical_command(required)
    if actual is None or expected is None:
        parsed = parse_validation_shell(executed)
        return (
            executed.strip() == required.strip()
            and bool(parsed.segments)
            and len(parsed.segments) == len(parsed.operators) + 1
            and all(operator == "&&" for operator in parsed.operators)
        )
    if actual == expected:
        return True
    actual_scope = _path_scope(actual, allow_scope_widening=True)
    expected_scope = _path_scope(expected)
    if actual_scope is None or expected_scope is None:
        return False
    actual_flags, actual_paths = actual_scope
    expected_flags, expected_paths = expected_scope
    if actual_flags != expected_flags:
        return False
    if not expected_paths:
        return not actual_paths
    return not _uncovered_targets(actual_paths, expected_paths)


def scope_difference(executed: str, required: str) -> str:
    """Name the arguments that keep an executed command from covering a required one."""
    actual = canonical_command(executed)
    expected = canonical_command(required)
    if actual is None or expected is None:
        return "shell expansion or command structure differs"
    actual_scope = _path_scope(actual, allow_scope_widening=True)
    expected_scope = _path_scope(expected)
    if actual_scope is None or expected_scope is None:
        no_paths: list[str] = []
        actual_scope = (shlex.split(actual), no_paths)
        expected_scope = (shlex.split(expected), no_paths)
    actual_flags, actual_paths = actual_scope
    expected_flags, expected_paths = expected_scope
    differences: list[str] = []
    extra = list((Counter(actual_flags) - Counter(expected_flags)).elements())
    missing = list((Counter(expected_flags) - Counter(actual_flags)).elements())
    if extra:
        differences.append(f"adds `{' '.join(extra)}`")
    if missing:
        differences.append(f"lacks `{' '.join(missing)}`")
    if not extra and not missing and actual_flags != expected_flags:
        differences.append("orders arguments differently")
    if expected_paths:
        uncovered = _uncovered_targets(actual_paths, expected_paths)
        if uncovered:
            differences.append(f"does not cover `{' '.join(uncovered)}`")
    elif actual_paths:
        differences.append(f"narrows the run to `{' '.join(actual_paths)}`")
    return "; ".join(differences) or "arguments differ"


def _uncovered_targets(actual_paths: list[str], expected_paths: list[str]) -> list[str]:
    return [
        requirement
        for requirement in expected_paths
        if not any(target_covers(path, requirement) for path in actual_paths)
    ]


_VALUELESS_OPTIONS = frozenset(
    {
        "-q",
        "-v",
        "-vv",
        "-s",
        "-x",
        "--check",
        "--diff",
        "--strict",
        "--fail-on-new",
        "--no-incremental",
        "--no-error-summary",
        "--no-fail-fast",
        "--no-header",
        "--all-targets",
        "--",
    }
)
# Pytest options that only change reporting or stop after early failures. A run that
# still exits 0 executed and passed every selected test, so they cannot change what
# passing evidence proves. -r<chars> is matched by prefix.
_PYTEST_REPORTING_OPTIONS = frozenset(
    {"-q", "-qq", "-v", "-vv", "-x", "--no-header", "--tb", "--color", "--durations", "--maxfail"}
)

# Compare runner names exactly. Adding a flag to a different runner requires an
# explicit safety decision instead of a shared string-normalization rule.
_PATH_SCOPE_RUNNERS = (
    ("cargo", "nextest", "run"),
    ("cargo", "clippy"),
    ("gobby", "test-types", "audit"),
    ("gobby", "test-quality", "audit"),
    ("ruff", "check"),
    ("ruff", "format"),
    ("pytest",),
    ("mypy",),
)
_RUNNER_REPORTING_OPTIONS: dict[tuple[str, ...], frozenset[str]] = {
    ("cargo", "nextest", "run"): frozenset(
        {"--status-level", "--failure-output", "--no-fail-fast"}
    ),
    ("ruff", "check"): frozenset({"--output-format"}),
    ("mypy",): frozenset({"--no-error-summary"}),
}
# These flags prove a strict superset only when they appear on the executed command.
_EXECUTED_SCOPE_WIDENING_OPTIONS: dict[tuple[str, ...], frozenset[str]] = {
    ("cargo", "clippy"): frozenset({"--all-targets"}),
}


def _path_scope(
    command: str, *, allow_scope_widening: bool = False
) -> tuple[list[str], list[str]] | None:
    parsed = parse_validation_shell(command)
    if len(parsed.segments) != 1:
        return None
    tokens = list(parsed.segments[0])
    start = 2 if tokens[:2] == ["uv", "run"] else 0
    if tokens[start : start + 2] in (["python", "-m"], ["python3", "-m"]):
        start += 2
    runner = _path_scope_runner(tokens, start)
    if runner is None:
        return None
    runner_words, end = runner
    raw_options: list[tuple[list[str], bool]] = []
    paths: list[str] = []
    # Unknown options are retained with their following word. This intentionally
    # declines broad-scope credit rather than interpreting an option value as a path.
    takes_value = False
    after_separator = False
    for token in tokens[end:]:
        if takes_value:
            raw_options[-1][0].append(token)
            takes_value = False
        elif token == "--":
            raw_options.append(([token], after_separator))
            after_separator = True
        elif token.startswith("-"):
            raw_options.append(([token], after_separator))
            # A short option with attached characters (-ra, -kname) carries its own value.
            attached = len(token) > 2 and token[1] != "-"
            takes_value = not attached and "=" not in token and token not in _VALUELESS_OPTIONS
        elif not any(char in token for char in "*?[]$"):
            path = posixpath.normpath(token)
            if path.startswith(("/", "../")) or path == "..":
                return None
            paths.append(path)
        else:
            raw_options.append(([token], after_separator))
    options = [
        option
        for option, is_after_separator in raw_options
        if not _ignorable_runner_option(
            runner_words,
            option[0],
            allow_scope_widening=allow_scope_widening,
            is_after_separator=is_after_separator,
        )
    ]
    # Distinct options compare in any order; repeats of one option keep their order.
    options.sort(key=lambda option: option[0].split("=", 1)[0])
    return tokens[:end] + [shlex.join(option) for option in options], paths


def _path_scope_runner(tokens: list[str], start: int) -> tuple[tuple[str, ...], int] | None:
    for runner in _PATH_SCOPE_RUNNERS:
        end = start + len(runner)
        if tuple(tokens[start:end]) == runner:
            return runner, end
    return None


def _ignorable_runner_option(
    runner: tuple[str, ...],
    option: str,
    *,
    allow_scope_widening: bool,
    is_after_separator: bool,
) -> bool:
    if is_after_separator:
        return False
    if runner == ("pytest",) and _pytest_reporting_option(option):
        return True
    name = option.split("=", 1)[0]
    if name in _RUNNER_REPORTING_OPTIONS.get(runner, frozenset()):
        return True
    return allow_scope_widening and name in _EXECUTED_SCOPE_WIDENING_OPTIONS.get(
        runner, frozenset()
    )


def _pytest_reporting_option(option: str) -> bool:
    name = option.split("=", 1)[0]
    return name in _PYTEST_REPORTING_OPTIONS or name.startswith("-r")
