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
    _substitution_end,
    backtick_body,
    scan_shell_command,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks._shell_syntax import joined_prefix_end
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
    literal_reference: bool = False,
) -> tuple[bool, _Modes]:
    if depth >= SHELL_WRAPPER_DEPTH:
        return literal_reference or bool(token.search(source)), modes
    literal_reference = literal_reference or bool(token.search(source))
    try:
        original = scan_shell_command(source, dialect=dialect)
    except ValueError:
        return literal_reference, modes
    literal_reference = literal_reference or any(
        token.search(word.value) for word in original.tokens
    )
    substitutions: list[str] = []
    if syntax == "program" and any(
        _parameter_sets(
            source[start:],
            variable,
            word_end=end - start,
            literal_reference=literal_reference,
            substitutions=substitutions,
        )
        for start, end in original.spans
    ):
        return True, modes
    if any(
        not heredoc.quoted
        and _parameter_sets(
            heredoc.text,
            variable,
            quoting="heredoc",
            literal_reference=literal_reference,
            substitutions=substitutions,
        )
        for heredoc in original.heredocs
    ):
        return True, modes
    for body in substitutions:
        if _program(
            body,
            variable,
            token,
            modes,
            dialect,
            depth + 1,
            literal_reference=literal_reference,
        )[0]:
            return True, modes
    # Bodies were checked with their original quotes above. Project them out of
    # the owner without importing their shell options or treating data as code.
    projected = _resolve_substitutions(source, depth, [], "subjects")
    source = projected[0]
    try:
        scan = scan_shell_command(source, dialect=dialect)
        literal_reference = literal_reference or any(
            token.search(word.value) for word in scan.tokens
        )
        if scan.heredocs:
            if any(
                not heredoc.quoted
                and _parameter_sets(
                    heredoc.text, variable, quoting="heredoc", literal_reference=literal_reference
                )
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
                    expression = " ".join(words)
                    if token.search(expression) or (
                        literal_reference and not _literal_arithmetic(expression)
                    ):
                        return True, modes
                elif words[0].endswith(")"):
                    if _program(
                        words[0][1:-1],
                        variable,
                        token,
                        modes,
                        dialect,
                        depth + 1,
                        literal_reference=literal_reference,
                    )[0]:
                        return True, modes
                elif token.search(text):
                    return True, modes
                continue
            # Complex control flow is not proof that a setter cannot execute.
            # In particular a skipped conditional disable must not clear a mode.
            if words[0] in {"if", "then", "else", "elif", "for", "while", "until", "case", "{"}:
                if literal_reference:
                    return True, modes
            environment_changes: list[tuple[str | None, str | None]] = []
            wrapper_effects: list[tuple[str, str]] = []
            command = _unwrap(
                words,
                environment_changes=environment_changes,
                stop_at_eval=True,
                effects=wrapper_effects,
            )
            # Input may become an argument or variable name through a pipe,
            # here-string/document, or file written earlier in this program.
            # Retain the original reference even after heredoc projection.
            if literal_reference and any(
                kind == "stdin"
                or (
                    kind == "environment" and not _raw_shell_word_is_literal(word.partition("=")[0])
                )
                for kind, word in wrapper_effects
            ):
                return True, modes
            # An assigned value can later become an environment name through
            # export/env expansion or a nameref. Do not require local dataflow
            # proof before rejecting a possible indirect setter.
            if any(
                value is not None and (key == variable or token.search(value))
                for key, value in environment_changes
            ):
                return True, modes
            if not command:
                continue
            if (
                literal_reference
                and command[0] != "["
                and not _raw_shell_word_is_literal(command[0])
            ):
                return True, modes
            name, args = shell_command_name(command[0]), command[1:]
            if literal_reference and name in {"read", "readarray", "mapfile", "getopts"}:
                return True, modes
            if (
                syntax == "program"
                and any(mode.keyword for mode in modes)
                and any(
                    _assignment_name(raw_words[id(word)].replace("\\\n", "")) == variable
                    for word in strip_input_redirections(strip_output_redirections(stage))
                )
            ):
                return True, modes
            if _builtin_sets(name, args, variable, token, literal_reference=literal_reference):
                return True, modes
            stage_modes = _shellopts_export(name, args, modes)
            if name in {"set", "setopt", "unsetopt"}:
                stage_modes = _keyword_options(name, args, modes)
            deferred: list[str] = []
            if name == "alias":
                deferred = [word.partition("=")[2] for word in args if "=" in word]
                if literal_reference and any(
                    "=" not in word and not _raw_shell_word_is_literal(word) for word in args
                ):
                    return True, modes
            elif name == "trap":
                operands = args[1:] if args[:1] == ["--"] else args
                if operands and operands[0] not in {"-p", "-l", "-"}:
                    deferred = operands[:1]
            for body in deferred:
                if _program(
                    body,
                    variable,
                    token,
                    modes,
                    dialect,
                    depth + 1,
                    literal_reference=literal_reference,
                )[0]:
                    return True, modes
            if name == "eval":
                found, stage_modes = _program(
                    " ".join(args),
                    variable,
                    token,
                    modes,
                    dialect,
                    depth + 1,
                    literal_reference=literal_reference,
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
                        literal_reference=literal_reference,
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
    raw_word: str,
    variable: str,
    *,
    quoting: Literal["word", "heredoc"] = "word",
    word_end: int | None = None,
    literal_reference: bool = False,
    substitutions: list[str] | None = None,
) -> bool:
    """Recognize assignment expansions only where shell quoting permits expansion."""
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
    skip_until = 0
    for index, char in enumerate(raw_word):
        if word_end is not None and index >= word_end:
            break
        if index < skip_until:
            continue
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quoting == "word" and char == quote:
            quote = None
        elif quoting == "word" and char in {"'", '"'} and quote is None:
            quote = char
        elif quote != "'" and (
            char == "`"
            or joined_prefix_end(raw_word, index, "$(") is not None
            or (
                quote is None
                and char in "<>"
                and joined_prefix_end(raw_word, index, char + "(") is not None
            )
        ):
            tick = char == "`"
            opening = index + 1 if tick else joined_prefix_end(raw_word, index, char + "(")
            assert opening is not None
            try:
                end = _substitution_end(raw_word, opening, tick, 0)
            except ValueError:
                return literal_reference
            body = raw_word[opening:end]
            if joined_prefix_end(raw_word, index, "$((") is not None:
                if literal_reference and not _literal_arithmetic(body):
                    return True
            elif substitutions is not None:
                substitutions.append(backtick_body(body) if tick else body)
            skip_until = end + 1
        elif char == "$" and quote != "'":
            if pattern.match(raw_word, index):
                return True
            if not literal_reference:
                continue
            if joined_prefix_end(raw_word, index, "$[") is not None:
                return True
            opening = joined_prefix_end(raw_word, index, "${")
            if opening is not None:
                end = raw_word.find("}", opening)
                body = raw_word[opening : end if end != -1 else len(raw_word)]
                # Indexed parameters and substring offsets can evaluate variables
                # as arithmetic even when the containing command only prints data.
                if body.startswith("!") or re.match(r"#?[A-Za-z_][A-Za-z0-9_]*\[", body):
                    return True
                offset_match = re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*?#$!-]):", body)
                if offset_match is None:
                    continue
                offset = body[offset_match.end() :]
                if offset[:1] not in {"-", "+", "=", "?"}:
                    if not _literal_arithmetic(offset):
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


def _literal_arithmetic(value: str) -> bool:
    """Prove arithmetic cannot follow a variable into another expression."""
    return re.fullmatch(r"[0-9\s.()+*/%<>&|^~!?:=,+-]+", value) is not None


def _builtin_sets(
    name: str,
    args: list[str],
    variable: str,
    token: re.Pattern[str],
    *,
    literal_reference: bool,
) -> bool:
    if name == "let":
        return any(
            token.search(word) or (literal_reference and not _literal_arithmetic(word))
            for word in args
        )
    if name in {"unset", "test", "["} and literal_reference:
        if (name == "unset" or "-v" in args) and any("[" in word for word in args):
            return True
    if name in {"read", "readarray", "mapfile", "getopts", "set"}:
        return any(
            token.search(word) or (literal_reference and not _raw_shell_word_is_literal(word))
            for word in args
        )
    if name == "alias":
        return any("=" in word and token.search(word) for word in args)
    if name == "trap":
        return bool(args and args[0] not in {"-p", "-l", "-"} and token.search(" ".join(args)))
    if name == "printf":
        for index, option in enumerate(args):
            if option == "--" or not option.startswith("-"):
                break
            if option.startswith("-v"):
                target = option[2:] or next(iter(args[index + 1 :]), "")
                if (
                    token.search(target)
                    or (literal_reference and not _raw_shell_word_is_literal(target))
                    or any(token.search(value) for value in args[index + 1 :])
                ):
                    return True
    if name not in {"export", "declare", "typeset", "local", "readonly", "integer", "float"}:
        return False
    nameref = False
    arithmetic = name in {"integer", "float"}
    options = True
    for word in args:
        if options and word == "--":
            options = False
            continue
        if options and word.startswith(("-", "+")):
            if name in {"declare", "typeset"} and any(flag in word[1:] for flag in "pf"):
                return False
            if name == "declare" and "F" in word[1:]:
                return False
            if name in {"declare", "typeset", "local"} and "n" in word[1:]:
                nameref = word.startswith("-")
            if name in {"declare", "typeset", "local", "integer", "float"} and "i" in word[1:]:
                arithmetic = word.startswith("-")
            if name == "typeset" and any(flag in word[1:] for flag in "EF"):
                arithmetic = word.startswith("-")
            continue
        options = False
        target, separator, value = word.partition("=")
        if literal_reference and (
            not _raw_shell_word_is_literal(target)
            or (nameref and (not separator or not _raw_shell_word_is_literal(value)))
            # The attribute persists: later assignments/printf may evaluate an
            # unknown value even when this initializer is a numeric literal.
            or arithmetic
        ):
            return True
    return any(
        word == variable or (_assignment_name(word) is not None and token.search(word))
        for word in args
    )
