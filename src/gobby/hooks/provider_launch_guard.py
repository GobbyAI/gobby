"""Bounded inspection of literal provider execution in observed shell commands.

This is a workflow guard, not a shell interpreter or an OS security boundary.
Keep quote provenance until executable contexts have been identified.
"""

from __future__ import annotations

import re
from collections.abc import Container
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

_PROVIDERS = frozenset({"codex", "claude", "droid", "grok", "qwen", "agy"})
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
    "qwen": {
        "auth": {},
        "channel": {},
        "extensions": {
            "disable": {},
            "enable": {},
            "install": {},
            "link": {},
            "list": {},
            "new": {},
            "uninstall": {},
            "update": {},
        },
        "hooks": {},
        "mcp": _MCP,
        "review": {},
        "serve": {},
        "sessions": {},
        "update": {},
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
            if char == "#" and (index == 0 or command[index - 1] in " \t\n;|&()"):
                end = command.find("\n", index)
                index = len(command) if end < 0 else end
                continue
            if char in "()":
                char = ";"
            if char == "\n":
                # Only the current logical line is rescanned. A trailing pipe
                # delays heredoc consumption until its command is complete.
                line = scan_shell_command("".join(output[line_start:]))
                tokens = line.tokens
                if tokens and tokens[-1].value in {"|", "&&", "||"}:
                    output.append(" ")
                    index += 1
                    continue
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


def option_word_count(option: str, value_options: Container[str]) -> int:
    """Words a leading option occupies, read as getopt reads it.

    A value option takes the next word unless its value is attached
    (``--chdir=/x``, ``-D/x``). In a short cluster (``-nD``) the first value
    option takes the rest of the cluster, or the next word when it ends it.
    """
    if option in value_options:
        return 2
    if option.startswith("--"):
        return 1
    for index in range(1, len(option)):
        if "-" + option[index] in value_options:
            return 2 if index == len(option) - 1 else 1
    return 1


def _env_split_string(
    option: str, rest: list[str], value_options: Container[str]
) -> list[str] | None:
    """The words env's -S option reads as a command string, or None without -S.

    ``-S`` may end a short cluster (``-iS cmd``) or carry its string attached
    (``-Scmd``, ``--split-string=cmd``); an earlier value option in the cluster
    takes the rest of it instead (``-uS`` unsets ``S``).
    """
    if option == "--split-string":
        return rest
    if option.startswith("--split-string="):
        return [option.split("=", 1)[1], *rest]
    if option.startswith("--"):
        return None
    for index in range(1, len(option)):
        letter = "-" + option[index]
        if letter == "-S":
            attached = option[index + 1 :]
            return [attached, *rest] if attached else rest
        if letter in value_options:
            return None
    return None


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
                "env": {"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"},
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
                    "--user",
                    "--group",
                    "--host",
                    "--prompt",
                    "--chdir",
                    "--chroot",
                    "--close-from",
                    "--command-timeout",
                    "--other-user",
                },
                "time": {"-f", "-o", "--format", "--output"},
                "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
                "xargs": {
                    "-I",
                    "-J",
                    "-n",
                    "-P",
                    "-L",
                    "-s",
                    "-E",
                    "-d",
                    "--max-args",
                    "--max-procs",
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
                    # env -S interprets its argument as a command string.
                    return ["sh", "-c", " ".join(script)]
                words = words[option_word_count(option, takes_value) :]
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
        or (provider in {"claude", "droid", "grok", "qwen"} and args == ["-v"])
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


def _piped_to_shell(tokens: list[ShellToken], end: int) -> bool:
    while end < len(tokens) and tokens[end].value == "|":
        start = end + 1
        end = start
        while end < len(tokens) and not _separator(tokens[end]):
            end += 1
        if _shell_stdin(_unwrap(_executable_words(tokens[start:end]))):
            return True
    return False


def _blocked(command: str, depth: int, dialect: ShellDialect = "bash") -> bool:
    if depth > _MAX_DEPTH or len(command) > _MAX_LENGTH:
        return True
    try:
        prepared, expansions = _prepare(command, depth)
        if any(_blocked(body, depth + 1, dialect) for body in expansions):
            return True
        scan = scan_shell_command(prepared, dialect=dialect)
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
            piped = _piped_to_shell(scan.tokens, end)
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
        return False
    except ValueError:
        # A malformed or over-deep command cannot earn an administrative exemption.
        return True
