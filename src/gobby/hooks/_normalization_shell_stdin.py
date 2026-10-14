"""Prove the program text supplied to a shell on stdin."""

import shlex

from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_env import UNRESOLVED_WRAPPER
from gobby.hooks._normalization_segments import _ShellSegment
from gobby.hooks._normalization_shell import (
    ShellScan,
    ShellToken,
    _strip_shell_wrappers,
    is_shell_input_redirection_token,
    is_unquoted_shell_control_token,
    shell_token_values,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS
from gobby.hooks.shell_execution import SCRIPT_BODY_LIMIT, shell_execution


def shell_stdin_arguments(
    segment: _ShellSegment,
    previous: _ShellSegment | None,
    scan: ShellScan,
    raw: dict[int, str],
) -> tuple[list[ShellToken], list[str], dict[int, str]]:
    """Classify proven stdin like ``-c``; keep opaque input an unknown write.

    The replacement is local to classification. The original scan still owns
    directory transitions, source spans, heredocs and pipeline bookkeeping.
    """
    tokens = segment.tokens
    plain = strip_input_redirections(strip_output_redirections(tokens))
    # xargs consumes stdin to build argv; it does not forward that stream as
    # the shell's program. Other execution wrappers preserve stdin.
    parts = _unwrap(shell_token_values(plain), stop_at_xargs=True)
    if parts and shell_command_name(parts[0]) == "xargs" and shell_execution(parts) is not None:
        # Runtime argv may add -c/program text or replace an existing -c word.
        # Stopping the stdin proof must not turn that program into plain execution.
        return _replace_stdin_program(tokens, [ShellToken(UNRESOLVED_WRAPPER)], raw)
    inputs = [
        index for index, token in enumerate(tokens) if is_shell_input_redirection_token(token)
    ]
    if not parts or shell_command_name(parts[0]) not in _SHELLS:
        return tokens, _strip_shell_wrappers(shell_token_values(tokens)), raw
    execution = shell_execution(parts, stdin=bool(inputs) or segment.separator_before == "|")
    if (
        not inputs
        and segment.separator_before != "|"
        or execution is None
        or execution.command is not None
        or execution.script_word is not None
    ):
        return tokens, _strip_shell_wrappers(shell_token_values(tokens)), raw

    source: str | None = None
    if len(inputs) == 1:
        index = inputs[0]
        if index + 1 < len(tokens):
            operand = tokens[index + 1]
            operator = tokens[index].value
            descriptor = operator[: len(operator) - len(operator.lstrip("0123456789"))]
            operator = operator.lstrip("0123456789") if descriptor in {"", "0"} else ""
            if operator == "<<<" and _raw_shell_word_is_literal(raw[id(operand)]):
                source = operand.value + "\n"
            elif operator in {"<<", "<<-"}:
                bodies = [body for body in scan.heredocs if scan.tokens[body.opener] is operand]
                if len(bodies) == 1 and bodies[0].terminated:
                    body = bodies[0]
                    # Unquoted heredocs expand even single-quoted text. Without
                    # expansion or escape syntax the recorded text is exact.
                    if body.quoted or not any(char in body.text for char in "$`\\"):
                        source = body.text + "\n"
    elif (
        not inputs
        and segment.separator_before == "|"
        and not segment.stderr_pipe
        and previous is not None
    ):
        source = _literal_stdout(previous.tokens, raw)

    # Startup/configuration options and positional parameters need their own
    # proof. Simple shell flags do not change how literal program text is read.
    if not _safe_stdin_options(parts[1:], shell_command_name(parts[0])):
        source = None
    if source is None or len(source) > SCRIPT_BODY_LIMIT:
        replacement = [ShellToken(UNRESOLVED_WRAPPER)]
    else:
        replacement = [ShellToken(parts[0]), ShellToken("-c"), ShellToken(source, quoted=True)]
    return _replace_stdin_program(tokens, replacement, raw)


def _replace_stdin_program(
    tokens: list[ShellToken], replacement: list[ShellToken], raw: dict[int, str]
) -> tuple[list[ShellToken], list[str], dict[int, str]]:
    # Retain output redirections so the body and its output destinations both
    # participate in the mutation scope. Input data is never a file operand.
    raw = {**raw, **{id(token): shlex.quote(token.value) for token in replacement}}
    outputs = strip_input_redirections(tokens)
    positional = strip_output_redirections(outputs)
    # Redirections can precede the command, so select them by token identity.
    positional_ids = {id(token) for token in positional}
    replacement.extend(token for token in outputs if id(token) not in positional_ids)
    return replacement, shell_token_values(replacement), raw


def _safe_stdin_options(options: list[str], shell: str) -> bool:
    """Consume simple flags and safe option operands without treating them as files."""
    index = 0
    while index < len(options):
        option = options[index]
        index += 1
        if option in {"--", "-", "--norc", "--noprofile"}:
            continue
        if not option.startswith("-") or option.startswith("--"):
            return False
        flags = option[1:]
        if not flags or not set(flags) <= set("seuxvoO"):
            return False
        if "O" in flags and shell not in {"bash", "sh"}:
            return False
        if "o" in flags or "O" in flags:
            # Value-taking flags must end the cluster. Startup and grammar
            # options outside this proof remain unknown-scope writes.
            names = (
                {"pipefail", "errexit", "nounset", "xtrace", "verbose"}
                if flags.endswith("o")
                else {"nullglob", "failglob", "globstar", "extglob"}
            )
            if (
                flags[-1] not in "oO"
                or any(flag in "oO" for flag in flags[:-1])
                or index >= len(options)
                or options[index] not in names
            ):
                return False
            index += 1
    return True


def _literal_stdout(tokens: list[ShellToken], raw: dict[int, str]) -> str | None:
    if any(is_unquoted_shell_control_token(token) for token in tokens):
        return None
    parts = _unwrap(shell_token_values(tokens), stop_at_xargs=True)
    if not parts or not all(_raw_shell_word_is_literal(raw[id(token)]) for token in tokens):
        return None
    command, *arguments = parts
    if shell_command_name(command) == "echo":
        if any("\\" in argument for argument in arguments):
            return None
        newline = "\n"
        if arguments[:1] == ["-n"]:
            arguments = arguments[1:]
            newline = ""
        if arguments and arguments[0].startswith("-"):
            return None
        return " ".join(arguments) + newline
    if shell_command_name(command) == "printf":
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return _literal_printf(arguments)
    return None


def _literal_printf(arguments: list[str]) -> str | None:
    if not arguments:
        return None
    format_text, *values = arguments
    if format_text.startswith("-"):
        return None
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    chunks: list[str | None] = []
    index = 0
    while index < len(format_text):
        char = format_text[index]
        if char in {"%", "\\"}:
            index += 1
            if index == len(format_text):
                return None
            next_char = format_text[index]
            if char == "%":
                if next_char not in {"%", "s"}:
                    return None
                chunks.append(None if next_char == "s" else "%")
            else:
                if next_char not in escapes:
                    return None
                chunks.append(escapes[next_char])
        else:
            chunks.append(char)
        index += 1
    slots = chunks.count(None)
    if values and not slots:
        return None
    result: list[str] = []
    consumed = 0
    size = 0
    while True:
        for chunk in chunks:
            if chunk is None:
                chunk = values[consumed] if consumed < len(values) else ""
                consumed += 1
            size += len(chunk)
            if size > SCRIPT_BODY_LIMIT:
                return None
            result.append(chunk)
        if consumed >= len(values):
            return "".join(result)
