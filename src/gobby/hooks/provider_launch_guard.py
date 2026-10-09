"""Bounded inspection of literal provider execution in observed shell commands.

This is a workflow guard, not a shell interpreter or an OS security boundary.
Keep quote provenance until executable contexts have been identified.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Collection, Mapping
from typing import Any

from gobby.hooks._ansi_c import SHELL_DIALECTS, ShellDialect
from gobby.hooks._normalization_shell import (
    ShellToken,
    _get_command_text,
    _skip_heredocs,
    _substitution_end,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    is_shell_tool,
    scan_shell_command,
)
from gobby.hooks._python_pipeline_classifier import (
    _PYTHON_PROCESS_CALLS,
    _call_name,
    _imported_bindings,
    _inline_interpreter_parts,
)

_PROVIDERS = frozenset({"codex", "claude", "droid", "grok", "agy"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_MAX_DEPTH = 24
_MAX_LENGTH = 131_072

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


def _prepare(command: str, depth: int, *, data: bool = False) -> tuple[str, list[str]]:
    """Mask expansions/comments and separate grouping for the shared shell scanner.

    Heredocs stay untouched for scan_shell_command to associate with their
    consumer. Unquoted data heredocs expand substitutions even inside quotes.
    """
    output: list[str] = []
    expansions: list[str] = []
    quote = ""
    index = 0
    line_start = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            output.append(command[index : index + 2])
            index += 2
            continue
        if quote not in {"'", "$"} and (
            command.startswith("$(", index)
            or char == "`"
            or (not data and not quote and command[index : index + 2] in {"<(", ">("})
        ):
            tick = char == "`"
            start = index + (1 if tick else 2)
            end = _substitution_end(command, start, tick, depth)
            expansions.append(command[start:end])
            output.append("__gobby_expansion__")
            index = end + 1
            continue
        if not data and not quote and command.startswith("$'", index):
            # ANSI-C `$'...'` ("$" state): a backslash escapes the next character.
            quote = "$"
            output.append("$'")
            index += 2
            continue
        if not data and char in "\"'":
            if not quote:
                quote = char
            elif quote == char or (quote == "$" and char == "'"):
                quote = ""
        elif not data and not quote:
            if char == "#" and (index == 0 or command[index - 1] in " \t\n;|&("):
                end = command.find("\n", index)
                index = len(command) if end < 0 else end
                continue
            if char in "()":
                char = ";"
            if char == "\n":
                # Heredoc bodies begin at the newline even after a trailing
                # pipe or logical operator; the continuation follows the body.
                line = scan_shell_command("".join(output[line_start:]))
                tokens = line.tokens
                output.append(char)
                body_start = index + 1
                index = _skip_heredocs(command, tokens, body_start)
                output.append(command[body_start:index])
                line_start = len(output)
                continue
        output.append(char)
        index += 1
    return "".join(output), expansions


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


def option_word_count(
    option: str,
    value_options: Collection[str],
    *,
    infer_long_options: bool = False,
    flag_options: Collection[str] = (),
) -> int:
    """Words a leading option occupies, read as getopt reads it.

    A value option takes the next word unless its value is attached
    (``--chdir=/x``, ``-D/x``). In a short cluster (``-nD``) the first value
    option takes the rest of the cluster, or the next word when it ends it.
    Long-option inference is opt-in: getopt permits prefixes, clap does not.
    An exact flag wins over a prefix of a longer value option.
    """
    if option in value_options:
        return 2
    if option.startswith("--"):
        if infer_long_options and "=" not in option and option not in flag_options:
            return 2 if any(value.startswith(option) for value in value_options) else 1
        return 1
    for index in range(1, len(option)):
        if "-" + option[index] in value_options:
            return 2 if index == len(option) - 1 else 1
    return 1


def _env_split_string(
    option: str, rest: list[str], value_options: Collection[str]
) -> list[str] | None:
    """The words env's -S option reads as a command string, or None without -S.

    ``-S`` may end a short cluster (``-iS cmd``) or carry its string attached
    (``-Scmd``, ``--split-string=cmd``); an earlier value option in the cluster
    takes the rest of it instead (``-uS`` unsets ``S``).
    """
    if option.startswith("--"):
        flag, separator, attached = option.partition("=")
        if len(flag) > 2 and "--split-string".startswith(flag):
            return [attached, *rest] if separator else rest
        return None
    for index in range(1, len(option)):
        letter = "-" + option[index]
        if letter == "-S":
            attached = option[index + 1 :]
            return [attached, *rest] if attached else rest
        if letter in value_options:
            return None
    return None


def _split_env_argv(string: str) -> list[str]:
    """Read literal env -S words, preserving operators and escaped whitespace as data.

    env has its own quoting/escape grammar, including ``\\_`` separators.
    Dynamic expansions and malformed strings retain the conservative shell reading.
    """
    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    index = 0
    escapes = {"f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while index < len(string):
        char = string[index]
        index += 1
        if char in " \t\n\v\f\r" and not quote:
            if started:
                words.append("".join(word))
                word = []
                started = False
            continue
        if char == "#" and not started:
            break
        if char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
            started = True
            continue
        if char == "\\" and (quote != "'" or string[index : index + 1] in {"\\", "'"}):
            if index == len(string):
                raise ValueError("Trailing env -S escape")
            char = string[index]
            index += 1
            if char == "_" and not quote:
                if started:
                    words.append("".join(word))
                    word = []
                    started = False
                continue
            if char == "c" and not quote:
                break
            if char == "_" and quote == '"':
                char = " "
            elif char in escapes:
                char = escapes[char]
            elif char not in "\"'#$\\":
                raise ValueError("Invalid env -S escape")
        elif char == "$" and quote != "'":
            raise ValueError("Dynamic env -S expansion")
        word.append(char)
        started = True
    if quote:
        raise ValueError("Unterminated env -S quote")
    if started:
        words.append("".join(word))
    return words


_SUDO_FLAG_OPTIONS = frozenset(
    {
        "--background",
        "--preserve-env",  # Optional operands must be attached with '='.
        "--edit",
        "--set-home",
        "--login",
        "--remove-timestamp",
        "--list",
        "--preserve-groups",
        "--shell",
        "--validate",
        "--askpass",
        "--bell",
        "--help",
        "--reset-timestamp",
        "--no-update",
        "--non-interactive",
        "--stdin",
        "--version",
    }
)


def _unwrap(words: list[str]) -> list[str]:
    """Common literal execution wrappers; never treat query operands as launches."""
    while words:
        name = words[0].rsplit("/", 1)[-1]
        if _ASSIGNMENT.match(words[0]) or name in {
            "!",
            "{",
            "then",
            "do",
            "else",
            "elif",
            "if",
            "while",
            "until",
        }:
            words = words[1:]
        elif name in {
            "command",
            "builtin",
            "exec",
            "nohup",
            "time",
            "env",
            "nice",
            "timeout",
            "sudo",
            "setsid",
            "stdbuf",
            "xargs",
        }:
            words = words[1:]
            takes_value = {
                # BSD env adds -P; GNU env adds -a/--argv0. -S is handled below.
                "env": {
                    "-u",
                    "--unset",
                    "-C",
                    "--chdir",
                    "-P",
                    "-a",
                    "--argv0",
                    "--env0-from",
                    "--quoting-style",
                },
                "exec": {"-a"},
                "nice": {"-n", "--adjustment"},
                "timeout": {"-s", "--signal", "-k", "--kill-after"},
                "sudo": {
                    "-u",
                    "-g",
                    "-h",
                    "-p",
                    "-C",
                    "-D",
                    "-R",
                    "-T",
                    "-U",
                    "-a",
                    "-c",
                    "-r",
                    "-t",
                    "--user",
                    "--group",
                    "--host",
                    "--prompt",
                    "--chdir",
                    "--chroot",
                    "--close-from",
                    "--command-timeout",
                    "--other-user",
                    "--auth-type",
                    "--login-class",
                    "--role",
                    "--type",
                },
                "time": {"-f", "-o", "--format", "--output"},
                "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
                "xargs": {
                    "-a",
                    "-I",
                    "-J",
                    "-n",
                    "-P",
                    "-L",
                    "-s",
                    "-E",
                    "-d",
                    "-R",
                    "-S",
                    "--arg-file",
                    "--max-args",
                    "--max-chars",
                    "--max-procs",
                    "--process-slot-var",
                    "--delimiter",
                },
            }.get(name, set())
            while words and words[0].startswith("-"):
                option = words[0]
                if option == "--":
                    words = words[1:]
                    break
                if name == "command" and any(flag in option for flag in "vV"):
                    return []
                script = (
                    _env_split_string(option, words[1:], takes_value) if name == "env" else None
                )
                if script is not None:
                    if not script:
                        return []
                    try:
                        words = [*_split_env_argv(script[0]), *script[1:]]
                    except ValueError:
                        return ["sh", "-c", " ".join(script)]
                    # Split words may themselves contain env options (shebang form).
                    continue
                count = option_word_count(
                    option,
                    takes_value,
                    infer_long_options=True,
                    flag_options=_SUDO_FLAG_OPTIONS if name == "sudo" else (),
                )
                words = words[count:]
            if name == "timeout" and words:
                words = words[1:]
        elif name == "eval":
            return ["sh", "-c", " ".join(words[1:])]
        else:
            break
    return words


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
    if not words or words[0].rsplit("/", 1)[-1] not in _SHELLS:
        return False
    args = iter(words[1:])
    for word in args:
        if word in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            next(args, None)
            continue
        if word.startswith("-"):
            if not word.startswith("--") and "c" in word:
                return False
            if not word.startswith("--") and "s" in word:
                return True
        else:
            return False
    return True


def _piped_to_shell(tokens: list[ShellToken], end: int, *, python: bool = False) -> bool:
    while end < len(tokens) and tokens[end].value == "|":
        start = end + 1
        while start < len(tokens) and not tokens[start].quoted and tokens[start].value == "\n":
            start += 1
        end = start
        while end < len(tokens) and not _separator(tokens[end]):
            end += 1
        words = _unwrap(_executable_words(tokens[start:end]))
        if _python_program(words)[1] if python else _shell_stdin(words):
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


def _blocked(command: str, depth: int, dialect: ShellDialect = "bash") -> bool:
    if depth > _MAX_DEPTH:
        return True
    try:
        prepared, expansions = _prepare(command, depth)
        if any(_blocked(body, depth + 1, dialect) for body in expansions):
            return True
        scan = scan_shell_command(prepared, dialect=dialect)
        inspected_length = 0
        start = 0
        for end in range(len(scan.tokens) + 1):
            if end < len(scan.tokens) and not _separator(scan.tokens[end]):
                continue
            segment = scan.tokens[start:end]
            words = _unwrap(_executable_words(segment))
            bodies = [body for body in scan.heredocs if start <= body.opener < end]
            start = end + 1
            if not words:
                continue
            name = words[0].rsplit("/", 1)[-1]
            # A named shell decodes the script it runs in its own dialect.
            inner: ShellDialect = "zsh" if name == "zsh" else "bash"
            if name in _PROVIDERS and not _administration(words[1:], name):
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
            piped = _piped_to_shell(scan.tokens, end)
            python_piped = _piped_to_shell(scan.tokens, end, python=True)
            if python_stdin:
                for index, token in enumerate(segment[:-1]):
                    if not token.quoted and token.value == "<<<":
                        if _python_launch(segment[index + 1].value, depth + 1, dialect):
                            return True
            if python_piped:
                if name in {"echo", "printf"}:
                    if any(_python_launch(value, depth + 1, dialect) for value in words[1:]):
                        return True
                if any(_python_launch(body.text, depth + 1, dialect) for body in bodies):
                    return True
            if piped and name in {"echo", "printf"}:
                if any(_blocked(value, depth + 1, dialect) for value in words[1:]):
                    return True
            if name in _SHELLS:
                for index, arg in enumerate(words[1:], 1):
                    if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                        # `--` ends the options; the script is the word after it.
                        script = words[index + 1 : index + 3]
                        if script[:1] == ["--"]:
                            script = script[1:]
                        if script and _blocked(script[0], depth + 1, inner):
                            return True
                        break
                # Literal here-strings are executable input as well.
                for index, token in enumerate(segment[:-1]):
                    if _shell_stdin(words) and not token.quoted and token.value == "<<<":
                        if _blocked(segment[index + 1].value, depth + 1, inner):
                            return True
            if _shell_stdin(words) or piped:
                body_dialect = inner if _shell_stdin(words) else dialect
                if any(_blocked(body.text, depth + 1, body_dialect) for body in bodies):
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
