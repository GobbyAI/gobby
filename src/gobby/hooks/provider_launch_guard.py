"""Bounded inspection of literal provider execution in observed shell commands.

This is a workflow guard, not a shell interpreter or an OS security boundary.
Keep quote provenance until executable contexts have been identified.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Literal

from gobby.hooks._ansi_c import SHELL_DIALECTS, ShellDialect
from gobby.hooks._normalization_shell import (
    ShellToken,
    _get_command_text,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    is_shell_tool,
    scan_shell_command,
)
from gobby.hooks._provider_shell_syntax import _prepare
from gobby.hooks._python_pipeline_classifier import (
    _PYTHON_PROCESS_CALLS,
    _call_name,
    _imported_bindings,
    _inline_interpreter_parts,
)
from gobby.hooks._sed_arguments import _sed_file_operands
from gobby.hooks._sed_program import sed_execution
from gobby.hooks._shell_execution_words import _unwrap

_PROVIDERS = frozenset({"codex", "claude", "droid", "grok", "agy"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_MAX_DEPTH = 24
_MAX_LENGTH = 131_072
_blocked_memo: ContextVar[dict[tuple[str, int, ShellDialect, str | None], bool] | None] = (
    ContextVar("_blocked_memo", default=None)
)

# Subcommands that answer a literal help request, by provider. A word maps to
# its child subcommands; an empty mapping is a leaf, which may take a prompt.
type _Subcommands = dict[str, _Subcommands]
_MCP: _Subcommands = {"add": {}, "get": {}, "list": {}, "remove": {}}
_SUBCOMMANDS: dict[str, _Subcommands] = {
    "codex": {
        "exec": {},
        "review": {},
        "login": {"status": {}},
        "logout": {},
        "mcp": {**_MCP, "login": {}, "logout": {}},
        "resume": {},
        "fork": {},
    },
    "claude": {
        "auth": {"login": {}, "logout": {}, "status": {}},
        "mcp": {
            **_MCP,
            "add-from-claude-desktop": {},
            "add-json": {},
            "reset-project-choices": {},
            "serve": {},
        },
        "plugin": {
            "disable": {},
            "enable": {},
            "install": {},
            "list": {},
            "marketplace": {"add": {}, "list": {}, "remove": {}, "update": {}},
            "uninstall": {},
            "update": {},
            "validate": {},
        },
        "doctor": {},
        "install": {},
        "update": {},
    },
    "droid": {
        "exec": {},
        "daemon": {},
        "search": {},
        "update": {},
        "mcp": _MCP,
        "plugin": {},
        "computer": {},
    },
    "grok": {
        "agent": {},
        "login": {},
        "logout": {},
        "mcp": _MCP,
        "models": {},
        "sessions": {},
        "doctor": {},
        "version": {},
    },
    "agy": {
        "agent": {},
        "agents": {},
        "mcp": _MCP,
        "models": {},
        "plugin": {},
        "plugins": {},
        "update": {},
    },
}
# Providers whose `help <subcommand>` form prints that subcommand's help.
_HELP_COMMAND_PROVIDERS = frozenset({"codex", "droid", "grok", "agy"})


def blocks_direct_provider_launch(tool_name: Any, tool_input: Any) -> bool:
    """Rule predicate, independent of session variables and launch preferences."""
    command = _get_command_text(tool_input)
    if not (is_shell_tool(tool_name) and command):
        return False
    # bash and zsh decode some `$'...'` escapes differently; block on either reading.
    return any(_blocked(command, 0, dialect) for dialect in SHELL_DIALECTS)


def _executable_words(tokens: list[ShellToken]) -> list[str]:
    words: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif is_fd_duplication_token(token):
            continue
        elif is_shell_input_redirection_token(token) or is_shell_output_redirection_token(token):
            skip = True
        else:
            words.append(token.value)
    return words


def _separator(token: ShellToken) -> bool:
    return not token.quoted and token.value in {";", "|", "&", "&&", "||", "\n"}


def _administration(args: list[str], provider: str) -> bool:
    # Only complete, literal forms: a help flag buried in a launch is not an exemption.
    if _help_request(args, provider):
        return True
    if provider == "claude" and args in [
        ["auth", "status"],
        ["auth", "status", "--json"],
        ["auth", "status", "--text"],
    ]:
        return True
    if provider == "grok" and args in [["version"], ["v"]]:
        return True
    return args == ["--version"] or (
        (provider == "codex" and args in [["login", "status"], ["-V"]])
        or (provider in {"claude", "droid", "grok"} and args == ["-v"])
    )


def _help_request(args: list[str], provider: str) -> bool:
    """Return whether ``args`` only ask for help on a chain of known subcommands."""
    wants_group = False
    if provider in _HELP_COMMAND_PROVIDERS and args[:1] == ["help"]:
        path = args[1:]
    elif args[-1:] in (["--help"], ["-h"]):
        path = args[:-1]
    elif args[-1:] == ["help"]:
        path, wants_group = args[:-1], True
    else:
        return False
    node = _SUBCOMMANDS[provider]
    for word in path:
        if word not in node:
            return False
        node = node[word]
    # A leaf subcommand can take `help` as its prompt; only a group answers it.
    return bool(node) or not wants_group


def _shell_stdin(words: list[str]) -> bool:
    if not words:
        return False
    name = words[0].rsplit("/", 1)[-1]
    if name in {"source", "."}:
        return words[1:2] == ["/dev/stdin"]
    if name not in _SHELLS:
        return False
    args = iter(words[1:])
    for word in args:
        if word in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            next(args, None)
            continue
        if word.startswith(("-", "+")):
            if word.startswith("-") and not word.startswith("--") and "c" in word:
                return False
            if word.startswith("-") and not word.startswith("--") and "s" in word:
                return True
        else:
            return word == "/dev/stdin"
    return True


def _piped_to_shell(
    tokens: list[ShellToken],
    end: int,
    expansions: list[str] | None = None,
    depth: int = 0,
    dialect: ShellDialect = "bash",
    *,
    python: bool = False,
    functions: Mapping[str, str] | None = None,
) -> bool:
    while end < len(tokens) and tokens[end].value == "|":
        start = end + 1
        while start < len(tokens) and not tokens[start].quoted and tokens[start].value == "\n":
            start += 1
        end = start
        while end < len(tokens) and not _separator(tokens[end]):
            end += 1
        if _segment_consumes(
            tokens[start:end], expansions or [], depth, dialect, python=python, functions=functions
        ):
            return True
    return False


def _python_program(words: list[str]) -> tuple[str | None, bool, list[str]]:
    """Return Python code, stdin mode and module argv, stopping at a file operand."""
    parts = _inline_interpreter_parts(words)
    if not parts and words[:2] == ["uv", "run"]:
        parts = next((words[index:] for index, word in enumerate(words) if _python_name(word)), [])
    if not parts or not _python_name(parts[0]):
        return None, False, []
    args = parts[1:]
    while args:
        option = args[0]
        if option == "-":
            return None, True, []
        if option == "--":
            return None, len(args) == 1 or args[1] == "-", []
        if option == "--check-hash-based-pycs":
            args = args[2:]
            continue
        if option.startswith("--") or not option.startswith("-"):
            return None, False, []
        consumed = 1
        for index, flag in enumerate(option[1:], 2):
            if flag in "hV?":
                return None, False, []
            if flag == "m":
                module_args = [option[index:], *args[1:]] if option[index:] else args[1:]
                return None, False, module_args
            if flag == "c":
                code = option[index:] or (args[1] if len(args) > 1 else None)
                return code, False, []
            if flag in "WX":
                consumed = 1 if option[index:] else 2
                break
            if flag not in "bBdEiIOPqRsSuvx":
                return None, False, []
        args = args[consumed:]
    return None, True, []


def _python_name(word: str) -> bool:
    return re.fullmatch(r"python(?:3(?:\.\d+)?)?", word.rsplit("/", 1)[-1]) is not None


def _python_skeleton(node: ast.AST | None, bindings: Mapping[str, str]) -> str:
    """Keep literal command text while unknown values cannot earn help exemptions."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_python_skeleton(value, bindings) for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _python_skeleton(node.left, bindings) + _python_skeleton(node.right, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            return node.left.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        literal_receiver = isinstance(receiver, ast.Constant) and isinstance(receiver.value, str)
        if node.func.attr == "format" and literal_receiver:
            return _python_skeleton(receiver, bindings)
        if node.args and isinstance(node.args[0], ast.List | ast.Tuple):
            words = [_python_skeleton(element, bindings) for element in node.args[0].elts]
            if _call_name(node, bindings) == "shlex.join":
                return shlex.join(words)
            if node.func.attr == "join" and isinstance(receiver, ast.Constant):
                if isinstance(receiver.value, str):
                    return receiver.value.join(words)
    return "__gobby_dynamic__"


def _python_launch(script: str, depth: int, dialect: ShellDialect) -> bool:
    """Inspect process-call operands, never arbitrary Python string literals."""
    try:
        return _analyze_python_launch(script, depth, dialect)
    except (MemoryError, RecursionError):
        return True


def _analyze_python_launch(script: str, depth: int, dialect: ShellDialect) -> bool:
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False
    bindings = _imported_bindings(tree)
    if bindings is None:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node, bindings)
        if name not in _PYTHON_PROCESS_CALLS:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        operand = node.args[0] if node.args else keywords.get("args", keywords.get("cmd"))
        shell_operand = keywords.get("shell")
        shell = name in {"os.system", "asyncio.create_subprocess_shell"} or (
            shell_operand is not None
            and not (
                isinstance(shell_operand, ast.Constant) and shell_operand.value in (False, None, 0)
            )
        )
        if name != "asyncio.create_subprocess_exec" and not isinstance(
            operand, ast.List | ast.Tuple
        ):
            value = _python_skeleton(operand, bindings)
            command = value if shell else shlex.join([value])
        else:
            operands = (
                node.args
                if name == "asyncio.create_subprocess_exec"
                else operand.elts
                if isinstance(operand, ast.List | ast.Tuple)
                else []
            )
            argv = [_python_skeleton(arg, bindings) for arg in operands]
            if not argv:
                continue
            command = argv[0] if shell else shlex.join(argv)
        executable = keywords.get("executable")
        executable_name = (
            executable.value
            if isinstance(executable, ast.Constant) and isinstance(executable.value, str)
            else None
        )
        if shell:
            command = shlex.join([executable_name or "/bin/sh", "-c", command])
        elif executable_name is not None:
            command = shlex.join([executable_name, *shlex.split(command)[1:]])
        if _blocked(command, depth + 1, dialect):
            return True
    return False


def _cat_stdin(words: list[str]) -> bool:
    return (
        bool(words)
        and words[0].rsplit("/", 1)[-1] == "cat"
        and ("-" in words[1:] or all(arg.startswith("-") for arg in words[1:]))
    )


def _process_output_to_shell(
    segment: list[ShellToken],
    expansions: list[str],
    depth: int,
    dialect: ShellDialect,
    *,
    python: bool = False,
    functions: Mapping[str, str] | None = None,
) -> bool:
    """Inspect every redirected descriptor conservatively, including fd forwarding."""
    words = _unwrap(_executable_words(segment))
    tee = bool(words) and words[0].rsplit("/", 1)[-1] == "tee"
    for index, token in enumerate(segment[1:], 1):
        redirect = segment[index - 1]
        match = re.fullmatch(r"__gobby_process_output_([0-9]+)__", token.value)
        if match is None or not (tee or is_shell_output_redirection_token(redirect)):
            continue
        expansion_index = int(match[1])
        if expansion_index >= len(expansions):
            continue
        if _stream_consumes(
            expansions[expansion_index], depth + 1, dialect, python=python, functions=functions
        ):
            return True
    return False


def _segment_consumes(
    segment: list[ShellToken],
    expansions: list[str],
    depth: int,
    dialect: ShellDialect,
    *,
    python: bool,
    functions: Mapping[str, str] | None = None,
) -> bool:
    words = _unwrap(_executable_words(segment))
    if not python and words and words[0].rsplit("/", 1)[-1] in {"sed", "gsed"}:
        if any(
            sed_execution(program).reads_pattern
            for program in _sed_file_operands(words, None).programs
        ):
            return True
    if words and functions and words[0] in functions:
        return _stream_consumes(
            functions[words[0]], depth + 1, dialect, python=python, functions=functions
        )
    if _python_program(words)[1] if python else _shell_stdin(words):
        return True
    script = _shell_script(words)
    if not python and script is not None and depth < _MAX_DEPTH:
        if _stream_consumes(script, depth + 1, dialect, python=False, functions=functions):
            return True
    for word in words:
        match = re.fullmatch(r"__gobby_group_([0-9]+)__", word)
        if match is not None and int(match[1]) < len(expansions):
            if _stream_consumes(
                expansions[int(match[1])], depth + 1, dialect, python=python, functions=functions
            ):
                return True
    return _process_output_to_shell(
        segment, expansions, depth, dialect, python=python, functions=functions
    )


def _stream_consumes(
    command: str,
    depth: int,
    dialect: ShellDialect,
    *,
    python: bool,
    functions: Mapping[str, str] | None = None,
) -> bool:
    if depth > _MAX_DEPTH:
        return True
    prepared, expansions = _prepare(command, depth)
    tokens = scan_shell_command(prepared, dialect=dialect).tokens
    start = 0
    for end in range(len(tokens) + 1):
        if end == len(tokens) or _separator(tokens[end]):
            if _segment_consumes(
                tokens[start:end], expansions, depth, dialect, python=python, functions=functions
            ):
                return True
            start = end + 1
    return False


def _process_input_consumer(
    segment: list[ShellToken],
    words: list[str],
    marker: str,
    output_consumer: Literal["shell", "python"] | None,
    expansions: list[str],
    depth: int,
    dialect: ShellDialect,
    functions: Mapping[str, str] | None = None,
) -> Literal["shell", "python"] | None:
    """Identify an input substitution used as stdin or the first script operand."""
    if marker in words:
        if words[0].rsplit("/", 1)[-1] == "cat" and output_consumer is not None:
            return output_consumer
        index = words.index(marker)
        prefix = words[:index]
        through_operand = words[: index + 1]
        if prefix and prefix[0].rsplit("/", 1)[-1] in {"source", "."} and len(prefix) == 1:
            return "shell"
        if _shell_stdin(prefix) and not _shell_stdin(through_operand):
            return "shell"
        if _python_program(prefix)[1] and not _python_program(through_operand)[1]:
            return "python"
    for index, token in enumerate(segment[1:], 1):
        redirect = segment[index - 1]
        if (
            token.value == marker
            and not redirect.quoted
            and _stdin_redirect(redirect.value, segment)
        ):
            if _cat_stdin(words) and output_consumer is not None:
                return output_consumer
            if _segment_consumes(
                segment, expansions, depth, dialect, python=False, functions=functions
            ):
                return "shell"
            if _python_program(words)[1]:
                return "python"
    return None


def _stdin_redirect(value: str, segment: list[ShellToken]) -> bool:
    """Resolve literal input descriptor aliases to the supplied input stream."""
    match = re.fullmatch(r"([0-9]*)(<|<<<|<<-?)", value)
    if match is None:
        return False
    descriptor = match[1] or "0"
    reachable = {"0"}
    for token in segment:
        alias = None if token.quoted else re.fullmatch(r"([0-9]*)<&([0-9]+)", token.value)
        if alias is not None and (alias[1] or "0") in reachable:
            reachable.add(alias[2])
    return descriptor in reachable


def _shell_script(words: list[str]) -> str | None:
    if not words or words[0].rsplit("/", 1)[-1] not in _SHELLS:
        return None
    for index, arg in enumerate(words[1:], 1):
        if arg == "--":
            break
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
            script = words[index + 1 :]
            if script[:1] == ["--"]:
                script = script[1:]
            return script[0] if script else None
    return None


def _literal_producer(tokens: list[ShellToken]) -> bool:
    words = _unwrap(_executable_words(tokens))
    return (
        bool(words)
        and not any(_separator(token) for token in tokens)
        and words[0].rsplit("/", 1)[-1] in {"echo", "printf"}
        and all("$" not in word and "__gobby_" not in word for word in words)
    )


def _sed_known_input(
    segment: list[ShellToken],
    expansions: list[str],
    incoming: list[ShellToken],
    payloads: list[str],
    depth: int,
) -> bool:
    if payloads and all("$" not in body for body in payloads):
        return True
    for index, token in enumerate(segment[:-1]):
        if token.quoted or not _stdin_redirect(token.value, segment):
            continue
        value = segment[index + 1].value
        if token.value.lstrip("0123456789") == "<<<" and "$" not in value:
            return True
        marker = re.fullmatch(r"__gobby_process_input_([0-9]+)__", value)
        if marker is not None and int(marker[1]) < len(expansions):
            prepared, _ = _prepare(expansions[int(marker[1])], depth + 1)
            if _literal_producer(scan_shell_command(prepared).tokens):
                return True
    return _literal_producer(incoming)


def _blocked(
    command: str,
    depth: int,
    dialect: ShellDialect = "bash",
    *,
    output_consumer: Literal["shell", "python"] | None = None,
) -> bool:
    memo = _blocked_memo.get()
    token = None
    if memo is None:
        memo = {}
        token = _blocked_memo.set(memo)
    try:
        key = (command, depth, dialect, output_consumer)
        if key not in memo:
            memo[key] = _evaluate_blocked(command, depth, dialect, output_consumer=output_consumer)
        return memo[key]
    finally:
        if token is not None:
            _blocked_memo.reset(token)


def _evaluate_blocked(
    command: str,
    depth: int,
    dialect: ShellDialect = "bash",
    *,
    output_consumer: Literal["shell", "python"] | None = None,
) -> bool:
    if depth > _MAX_DEPTH:
        return True
    try:
        prepared, expansions = _prepare(command, depth)
        if any(_blocked(body, depth + 1, dialect) for body in expansions):
            return True
        scan = scan_shell_command(prepared, dialect=dialect)
        inspected_length = 0
        persistent_shell = False
        persistent_python = False
        functions: dict[str, str] = {}
        start = 0
        for end in range(len(scan.tokens) + 1):
            if end < len(scan.tokens) and not _separator(scan.tokens[end]):
                continue
            segment = scan.tokens[start:end]
            incoming: list[ShellToken] = []
            if start > 0 and scan.tokens[start - 1].value == "|":
                previous = start - 2
                while previous >= 0 and not _separator(scan.tokens[previous]):
                    previous -= 1
                incoming = scan.tokens[previous + 1 : start - 1]
            raw_words = _executable_words(segment)
            words = _unwrap(raw_words)
            bodies = [body for body in scan.heredocs if start <= body.opener < end]
            start = end + 1
            if not words:
                if "exec" in raw_words[:2]:
                    persistent_shell |= _process_output_to_shell(
                        segment, expansions, depth, dialect
                    )
                    persistent_python |= _process_output_to_shell(
                        segment, expansions, depth, dialect, python=True
                    )
                continue
            name = words[0].rsplit("/", 1)[-1]
            # A named shell decodes the script it runs in its own dialect.
            inner: ShellDialect = "zsh" if name == "zsh" else "bash"
            piped = (
                output_consumer == "shell"
                or persistent_shell
                or _piped_to_shell(
                    scan.tokens, end, expansions, depth, dialect, functions=functions
                )
                or _process_output_to_shell(
                    segment, expansions, depth, dialect, functions=functions
                )
            )
            python_piped = (
                output_consumer == "python"
                or persistent_python
                or _piped_to_shell(
                    scan.tokens, end, expansions, depth, dialect, python=True, functions=functions
                )
                or _process_output_to_shell(
                    segment, expansions, depth, dialect, python=True, functions=functions
                )
            )
            producer_consumer = output_consumer
            if producer_consumer is None:
                producer_consumer = "shell" if piped else "python" if python_piped else None
            if name.endswith("()"):
                for word in words[1:]:
                    group = re.fullmatch(r"__gobby_group_([0-9]+)__", word)
                    if group is not None and int(group[1]) < len(expansions):
                        functions[name[:-2]] = expansions[int(group[1])]
                continue
            has_input_payload = bool(bodies) or any(
                not token.quoted and token.value.lstrip("0123456789") == "<<<" for token in segment
            )
            shell_input = has_input_payload and _segment_consumes(
                segment, expansions, depth, dialect, python=False, functions=functions
            )
            group_consumers: list[Literal["shell", "python"]] = []
            if piped:
                group_consumers.append("shell")
            if python_piped:
                group_consumers.append("python")
            consumers: list[Literal["shell", "python"] | None] = [None, *group_consumers]
            if name in functions and any(
                _blocked(functions[name], depth + 1, dialect, output_consumer=consumer)
                for consumer in consumers
            ):
                return True
            for token in segment:
                group = re.fullmatch(r"__gobby_group_([0-9]+)__", token.value)
                if group is not None and int(group[1]) < len(expansions):
                    for group_consumer in group_consumers:
                        if _blocked(
                            expansions[int(group[1])],
                            depth + 1,
                            dialect,
                            output_consumer=group_consumer,
                        ):
                            return True
                match = re.fullmatch(r"__gobby_process_input_([0-9]+)__", token.value)
                if match is None:
                    continue
                index = int(match[1])
                if index >= len(expansions):
                    continue
                marker = token.value
                consumer = _process_input_consumer(
                    segment, words, marker, producer_consumer, expansions, depth, dialect, functions
                )
                if consumer is not None and _blocked(
                    expansions[index], depth + 1, dialect, output_consumer=consumer
                ):
                    return True
            if name in _PROVIDERS and not _administration(words[1:], name):
                return True
            if name in {"sed", "gsed"}:
                operands = _sed_file_operands(words, None)
                executions = [sed_execution(program) for program in operands.programs]
                if any(
                    _blocked(script, depth + 1, dialect)
                    for execution in executions
                    for script in execution.commands
                ):
                    return True
                if any(execution.unknown_execution for execution in executions):
                    return True
                if any(execution.substitutes for execution in executions):
                    if any(execution.pattern_modified for execution in executions) or any(
                        word == "--null-data"
                        or (word.startswith("-") and not word.startswith("--") and "z" in word)
                        for word in words[1:]
                    ):
                        return True
                if any(execution.reads_pattern for execution in executions):
                    if any(path != "-" for path in operands.inputs) or not _sed_known_input(
                        segment, expansions, incoming, [body.text for body in bodies], depth
                    ):
                        return True
            # Budget executable words; code operands and heredoc payloads belong
            # to their consumers, whose process operands are checked recursively.
            python_script, python_stdin, _ = _python_program(words)
            inspected_length += sum(len(word) for word in words) - len(python_script or "")
            if python_script is not None and _python_launch(python_script, depth + 1, dialect):
                return True
            if python_stdin:
                if any(_python_launch(body.text, depth + 1, dialect) for body in bodies):
                    return True
            if _cat_stdin(words):
                for index, token in enumerate(segment[:-1]):
                    if not token.quoted and token.value in {"<<<", "0<<<"}:
                        value = segment[index + 1].value
                        if piped and _blocked(value, depth + 1, dialect):
                            return True
                        if python_piped and _python_launch(value, depth + 1, dialect):
                            return True
            if python_stdin:
                for index, token in enumerate(segment[:-1]):
                    if not token.quoted and token.value.lstrip("0123456789") == "<<<":
                        if _python_launch(segment[index + 1].value, depth + 1, dialect):
                            return True
            if python_piped:
                if name in {"echo", "printf"}:
                    if any(_python_launch(value, depth + 1, dialect) for value in words[1:]):
                        return True
                if any(_python_launch(body.text, depth + 1, dialect) for body in bodies):
                    return True
            if piped and name in {"echo", "printf"}:
                values = words[1:]
                if name == "echo":
                    values = [*values, " ".join(values)]
                # A delimiter printed by one command is only a fragment of the
                # downstream script; it does not itself launch an executable.
                if any(
                    value.strip() not in {"{", "}"}
                    and _blocked(value, depth + 1, dialect, output_consumer=output_consumer)
                    for value in values
                ):
                    return True
            if name in _SHELLS:
                for index, arg in enumerate(words[1:], 1):
                    if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                        # `--` ends the options; the script is the word after it.
                        script = words[index + 1 : index + 3]
                        if script[:1] == ["--"]:
                            script = script[1:]
                        if script and any(
                            _blocked(script[0], depth + 1, inner, output_consumer=consumer)
                            for consumer in consumers
                        ):
                            return True
                        break
            # Literal here-strings can feed shells inside a group or a -c script.
            if shell_input:
                for index, token in enumerate(segment[:-1]):
                    if (
                        not token.quoted
                        and token.value.lstrip("0123456789") == "<<<"
                        and _stdin_redirect(token.value, segment)
                    ):
                        if any(
                            _blocked(
                                segment[index + 1].value,
                                depth + 1,
                                inner,
                                output_consumer=consumer,
                            )
                            for consumer in consumers
                        ):
                            return True
            if shell_input or piped:
                body_dialect = inner if shell_input else dialect
                if any(
                    _blocked(body.text, depth + 1, body_dialect, output_consumer=consumer)
                    for body in bodies
                    for consumer in consumers
                ):
                    return True
            for body in bodies:
                if not body.quoted:
                    _, substitutions = _prepare(body.text, depth, data=True)
                    if any(_blocked(value, depth + 1, dialect) for value in substitutions):
                        return True
        return inspected_length > _MAX_LENGTH
    except ValueError:
        # A malformed or over-deep command cannot earn an administrative exemption.
        return True
