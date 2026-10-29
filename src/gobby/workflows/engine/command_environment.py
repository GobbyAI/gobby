"""Bounded environment-effect checks using the existing shell scanner.

Keyword assignment mode belongs to a shell program, not flattened match subjects.
Opaque executable bodies mentioning the protected variable remain conservative.
"""

import re
from dataclasses import dataclass, replace
from typing import Literal

from gobby.hooks._ansi_c import SHELL_DIALECTS, ShellDialect
from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_shell import (
    scan_shell_command,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _python_program
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH, shell_execution

from .command_matching import (
    _pipeline_stages,
    _resolve_substitutions,
    _shell_input_scripts,
    _split_segments,
    _stage_words,
    _wrapper_scripts,
    executable_command_subjects,
)
from .command_matching_heredoc import heredoc_free_text
from .command_matching_uv import _language_stage


@dataclass(frozen=True)
class _ShellOptions:
    keyword: bool = False
    exported_shellopts: bool = False


_Modes = frozenset[_ShellOptions]


def command_sets_environment(command: object, variable: str) -> bool:
    """Find possible setters while allowing literal command data and inspection."""
    if not isinstance(command, str) or not command.strip():
        return False
    token = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(variable)}(?![A-Za-z0-9_])")
    return any(
        _program(command, variable, token, frozenset({_ShellOptions()}), dialect, 0)[0]
        for dialect in SHELL_DIALECTS
    )


def _program(
    source: str,
    variable: str,
    token: re.Pattern[str],
    modes: _Modes,
    dialect: ShellDialect,
    depth: int,
    *,
    syntax: Literal["program", "argv"] = "program",
) -> tuple[bool, _Modes]:
    if depth >= SHELL_WRAPPER_DEPTH:
        return bool(token.search(source)), modes
    # Substitutions cannot change the parent's shell options. Their independently
    # executable bodies remain conservative; discard correlated data projections.
    projected = _resolve_substitutions(source, depth, [], "subjects")
    if any(token.search(body) for body in projected[1:]):
        return True, modes
    source = projected[0]
    try:
        scan = scan_shell_command(source, dialect=dialect)
        if scan.heredocs:
            if any(
                not heredoc.quoted and _parameter_sets(heredoc.text, variable, quoting="heredoc")
                for heredoc in scan.heredocs
            ):
                return True, modes
            owner = heredoc_free_text(source, 0, len(source), scan.heredocs)
            owner_subjects = set(executable_command_subjects(owner))
            if any(
                token.search(subject)
                for subject in executable_command_subjects(source)
                if subject not in owner_subjects
            ):
                return True, modes
            source = owner
            scan = scan_shell_command(source, dialect=dialect)
    except ValueError:
        return bool(token.search(source)), modes
    if syntax == "program" and any(
        _parameter_sets(source[start:end], variable) for start, end in scan.spans
    ):
        return True, modes
    raw_words = {
        id(word): source[start:end]
        for word, (start, end) in zip(scan.tokens, scan.spans, strict=True)
    }
    previous_end = 0
    for segment in _split_segments(scan.tokens):
        initial = modes
        conditional = any(
            word.value in {"&&", "||"}
            for word in scan.tokens[previous_end : segment.first]
            if not word.quoted
        )
        previous_end = segment.last + 1
        stages = _pipeline_stages(scan.tokens[segment.first : segment.last + 1])
        start = scan.spans[segment.first][0]
        end = scan.spans[segment.last][1]
        text = source[start:end]
        if any(token.search(script) for script in _shell_input_scripts(text)):
            return True, modes
        for index, stage in enumerate(stages):
            words = _stage_words(stage)
            if not words:
                continue
            # The scanner preserves complete parenthesized programs as words.
            # Arithmetic may assign; a subshell inherits options but returns none.
            if words[0].startswith("(") and not stage[0].quoted:
                if words[0].startswith("(("):
                    if token.search(" ".join(words)):
                        return True, modes
                elif words[0].endswith(")"):
                    if _program(words[0][1:-1], variable, token, modes, dialect, depth + 1)[0]:
                        return True, modes
                elif token.search(text):
                    return True, modes
                continue
            # Complex control flow is not proof that a setter cannot execute.
            # In particular a skipped conditional disable must not clear a mode.
            if words[0] in {"if", "then", "else", "elif", "for", "while", "until", "case", "{"}:
                if token.search(source):
                    return True, modes
            environment_changes: list[tuple[str | None, str | None]] = []
            command = _unwrap(words, environment_changes=environment_changes, stop_at_eval=True)
            if any(key == variable and value is not None for key, value in environment_changes):
                return True, modes
            if not command:
                continue
            name, args = shell_command_name(command[0]), command[1:]
            if (
                syntax == "program"
                and any(mode.keyword for mode in modes)
                and any(
                    _assignment_name(raw_words[id(word)].replace("\\\n", "")) == variable
                    for word in strip_input_redirections(strip_output_redirections(stage))
                )
            ):
                return True, modes
            if _builtin_sets(name, args, variable, token):
                return True, modes
            stage_modes = _shellopts_export(name, args, modes)
            if name in {"set", "setopt", "unsetopt"}:
                stage_modes = _keyword_options(name, args, modes)
            if name == "eval":
                found, stage_modes = _program(
                    " ".join(args), variable, token, modes, dialect, depth + 1
                )
                if found:
                    return True, modes
            else:
                execution = shell_execution(command)
                if execution is not None and execution.parse_only:
                    continue
                child_modes = frozenset(
                    mode if mode.exported_shellopts else _ShellOptions() for mode in modes
                )
                for key, value in environment_changes:
                    if key is None or (key == "SHELLOPTS" and value is None):
                        child_modes = frozenset({_ShellOptions()})
                    elif key == "SHELLOPTS" and value is not None:
                        child_modes = frozenset(
                            {_ShellOptions("keyword" in value.split(":"), True)}
                        )
                if name in _SHELLS:
                    if execution is not None and execution.keyword_mode is not None:
                        child_modes = frozenset(
                            replace(mode, keyword=execution.keyword_mode) for mode in child_modes
                        )
                child_dialect = "bash" if name in {"bash", "sh"} else dialect
                for script in _wrapper_scripts([words]):
                    if _program(
                        script,
                        variable,
                        token,
                        child_modes,
                        child_dialect,
                        depth + 1,
                        syntax="program" if name in _SHELLS | {"watch", "su", "ssh"} else "argv",
                    )[0]:
                        return True, modes
                if (
                    _language_stage(command)
                    or _python_program(command)[0] is not None
                    or name in {"node", "nodejs"}
                ) and token.search(" ".join(args)):
                    return True, modes
            if len(stages) == 1 or (dialect == "zsh" and index == len(stages) - 1):
                modes = stage_modes
        if conditional:
            modes |= initial
        if previous_end < len(scan.tokens) and scan.tokens[previous_end].value == "&":
            modes = initial
    return False, modes


def _parameter_sets(
    raw_word: str, variable: str, *, quoting: Literal["word", "heredoc"] = "word"
) -> bool:
    """Recognize assignment expansions only where shell quoting permits expansion."""
    if quoting == "word" and _raw_shell_word_is_literal(raw_word):
        return False
    continuation = r"(?:\\\n)*"
    pattern = re.compile(
        continuation.join(re.escape(char) for char in "${" + variable)
        + continuation
        + r":?"
        + continuation
        + "="
    )
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw_word):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quoting == "word" and char == quote:
            quote = None
        elif quoting == "word" and char in {"'", '"'} and quote is None:
            quote = char
        elif char == "$" and quote != "'" and pattern.match(raw_word, index):
            return True
    return False


def _assignment_name(word: str) -> str | None:
    name, separator, _ = word.partition("=")
    return name.removesuffix("+") if separator else None


def _keyword_options(name: str, args: list[str], modes: _Modes) -> _Modes:
    if name in {"setopt", "unsetopt"}:
        for word in args:
            option = word.lower().replace("_", "")
            if option in {"keyword", "nokeyword"}:
                modes = frozenset(
                    replace(mode, keyword=(name == "setopt") == (option == "keyword"))
                    for mode in modes
                )
        return modes
    index = 0
    while index < len(args):
        option = args[index]
        if option == "--" or not option.startswith(("-", "+")):
            break
        if option in {"-o", "+o"}:
            if args[index + 1 : index + 2] == ["keyword"]:
                modes = frozenset(replace(mode, keyword=option == "-o") for mode in modes)
            index += 2
            continue
        if not option.startswith(("--", "++")):
            if "k" in option[1:]:
                modes = frozenset(replace(mode, keyword=option[0] == "-") for mode in modes)
        index += 1
    return modes


def _shellopts_export(name: str, args: list[str], modes: _Modes) -> _Modes:
    """Carry the export attribute through same-shell declarations and eval."""
    if name not in {"export", "declare", "typeset", "local"}:
        return modes
    exported: bool | None = True if name == "export" else None
    index = 0
    while index < len(args):
        option = args[index]
        if option == "--":
            index += 1
            break
        if not option.startswith(("-", "+")):
            break
        if "p" in option[1:]:
            return modes
        if name == "export" and option.startswith("-") and "n" in option[1:]:
            exported = False
        if "x" in option[1:]:
            exported = option.startswith("-")
        index += 1
    if exported is not None and any(
        word == "SHELLOPTS" or _assignment_name(word) == "SHELLOPTS" for word in args[index:]
    ):
        return frozenset(replace(mode, exported_shellopts=exported) for mode in modes)
    return modes


def _builtin_sets(name: str, args: list[str], variable: str, token: re.Pattern[str]) -> bool:
    if name in {"read", "readarray", "mapfile", "getopts", "let", "integer", "float"}:
        return any(token.search(word) for word in args)
    if name == "printf":
        for index, option in enumerate(args):
            if option == "--" or not option.startswith("-"):
                break
            if option.startswith("-v"):
                target = option[2:] or next(iter(args[index + 1 :]), "")
                if token.search(target):
                    return True
    if name not in {"export", "declare", "typeset", "local", "readonly"}:
        return False
    if name in {"declare", "typeset"}:
        for option in args:
            if option == "--" or not option.startswith(("-", "+")):
                break
            if "p" in option[1:]:
                return False
    return any(word == variable or _assignment_name(word) == variable for word in args)
