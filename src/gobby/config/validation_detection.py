"""Configurable validation command detection."""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.config.validation_matchers import ValidationCommandMatcher, builtin_validation_matchers

logger = logging.getLogger(__name__)

PROJECT_VALIDATION_DETECTION_KEY = "validation_detection"
_ENV_ASSIGNMENT_RE_PREFIX = "="
_MAX_WRAPPER_NORMALIZATION_DEPTH = 8
_EVIDENCE_ENV_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_EVIDENCE_RTK_PREFIX = re.compile(r"^(uv\s+run\s+)?rtk\s+")
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
_NPX_OPTIONS_WITH_VALUES = ["--package", "-p", "--call", "-c", "--workspace", "-w"]
WrapperKind = Literal["prefix", "delimiter", "command_string"]


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
    bounded_inputs: bool = False

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
        _wrapper_rule(
            "npx", "npx", "prefix", ["npx"], strip_options_with_values=_NPX_OPTIONS_WITH_VALUES
        ),
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
                        bounded_inputs=matcher.bounded_inputs,
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
        for matcher in _builtin_matchers():
            if matcher.enabled and matcher.id not in disabled_ids:
                yield matcher
    for matcher in config.custom_matchers:
        if matcher.enabled:
            yield matcher


@lru_cache(maxsize=1)
def _builtin_matchers() -> tuple[ValidationCommandMatcher, ...]:
    """Construct fixed built-ins once; detection only reads their fields."""
    return tuple(builtin_validation_matchers())


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
        if wrapper.id == "nice":
            nice_command = _unwrap_nice_tokens(tokens)
            return ([nice_command], ()) if nice_command is not None else None
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


def _unwrap_nice_tokens(tokens: list[str]) -> list[str] | None:
    """Return the command executed by nice, with or without its -- separator."""
    if not tokens or not _matches_command_token(tokens[0], "nice"):
        return None
    cursor = 1
    if cursor < len(tokens) and tokens[cursor] == "-n":
        if cursor + 1 >= len(tokens) or re.fullmatch(r"[+-]?\d+", tokens[cursor + 1]) is None:
            return None
        cursor += 2
    elif cursor < len(tokens) and re.fullmatch(r"(?:-n[+-]?|-)\d+", tokens[cursor]):
        cursor += 1
    if cursor < len(tokens) and tokens[cursor] == "--":
        cursor += 1
    if cursor >= len(tokens) or tokens[cursor].startswith("-"):
        return None
    return tokens[cursor:]


def normalize_validation_evidence_command(command: str) -> str:
    """Remove exit-preserving prefixes and use the category check's nice grammar."""
    cursor = _evidence_skip_whitespace(command, 0)
    while True:
        next_cursor = _evidence_consume_cd_prefix(command, cursor)
        if next_cursor is None:
            next_cursor = _evidence_consume_export_prefix(command, cursor)
        if next_cursor is None:
            break
        cursor = _evidence_skip_whitespace(command, next_cursor)
    while _EVIDENCE_ENV_ASSIGNMENT_PREFIX.match(command, cursor):
        word_end = _evidence_shell_word_end(command, cursor)
        if word_end is None or word_end >= len(command) or not command[word_end].isspace():
            break
        cursor = _evidence_skip_whitespace(command, word_end)
    core = _EVIDENCE_RTK_PREFIX.sub(r"\1", command[cursor:].strip(), count=1)
    parsed = parse_shell_command(core)
    if len(parsed.segments) == 1 and not parsed.operators:
        unwrapped = _unwrap_nice_tokens(list(parsed.segments[0]))
        if unwrapped is not None:
            return shlex.join(unwrapped)
    return core


def _evidence_consume_cd_prefix(command: str, cursor: int) -> int | None:
    if not command.startswith("cd", cursor):
        return None
    name_end = cursor + 2
    if name_end >= len(command) or not command[name_end].isspace():
        return None
    path_start = _evidence_skip_whitespace(command, name_end)
    path_end = _evidence_shell_word_end(command, path_start)
    if path_end is None:
        return None
    operator_start = path_end
    while operator_start < len(command) and command[operator_start] in " \t\r":
        operator_start += 1
    if operator_start < len(command) and command[operator_start] == "\n":
        return operator_start + 1
    if not command.startswith("&&", operator_start):
        return None
    return operator_start + 2


def _evidence_consume_export_prefix(command: str, cursor: int) -> int | None:
    if not command.startswith("export", cursor):
        return None
    cursor += len("export")
    if cursor >= len(command) or not command[cursor].isspace():
        return None
    consumed_assignment = False
    while cursor < len(command):
        cursor = _evidence_skip_whitespace(command, cursor)
        if command.startswith("&&", cursor):
            return cursor + 2 if consumed_assignment else None
        if _EVIDENCE_ENV_ASSIGNMENT_PREFIX.match(command, cursor) is None:
            return None
        word_end = _evidence_shell_word_end(command, cursor)
        if word_end is None:
            return None
        cursor = word_end
        consumed_assignment = True
    return None


def _evidence_shell_word_end(command: str, start: int) -> int | None:
    cursor = start
    quote: str | None = None
    while cursor < len(command):
        char = command[cursor]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and cursor + 1 < len(command):
                cursor += 1
        elif char in {"'", '"'}:
            quote = char
        elif char == "\\" and cursor + 1 < len(command):
            cursor += 1
        elif char.isspace() or char in ";&|()":
            break
        cursor += 1
    if cursor == start or quote is not None:
        return None
    return cursor


def _evidence_skip_whitespace(command: str, cursor: int) -> int:
    while cursor < len(command) and command[cursor].isspace():
        cursor += 1
    return cursor


def _matcher_matches_segment(matcher: ValidationCommandMatcher, tokens: list[str]) -> bool:
    if not matcher.prefixes:
        return False
    for prefix in matcher.prefixes:
        prefix_tokens = safe_split(prefix)
        if not prefix_tokens or not _starts_with_command_prefix(tokens, prefix_tokens):
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
        if (prefix_tokens := safe_split(prefix))
        and _starts_with_command_prefix(tokens, prefix_tokens)
    ]
    arguments = tokens[max(prefix_lengths, default=0) :]
    if any(_tokens_include_arg(arguments, arg) for arg in matcher.evidence_weakening_args_any):
        return True
    for prefix in matcher.evidence_weakening_bare_args_after:
        prefix_tokens = safe_split(prefix)
        if not _starts_with_command_prefix(tokens, prefix_tokens):
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
    return token == expected or (
        "/" not in expected and token.rstrip("/").rsplit("/", 1)[-1] == expected
    )


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
