"""Argument parsing for shell execution wrappers."""

from collections.abc import Collection

from gobby.hooks._normalization_env import _ASSIGNMENT


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
    option: str,
    words: list[str],
    value_options: Collection[str],
    *,
    start: int = 0,
    environment_changes: list[tuple[str | None, str | None]] | None = None,
) -> list[str] | None:
    """The words env's -S option reads as a command string, or None without -S.

    ``-S`` may end a short cluster (``-iS cmd``) or carry its string attached
    (``-Scmd``, ``--split-string=cmd``); an earlier value option in the cluster
    takes the rest of it instead (``-uS`` unsets ``S``).
    """
    if option.startswith("--"):
        flag, separator, attached = option.partition("=")
        if environment_changes is not None:
            if flag == "--ignore-environment":
                environment_changes.append((None, None))
            elif flag == "--unset" and (separator or len(words) > start):
                environment_changes.append((attached if separator else words[start], None))
        if len(flag) > 2 and "--split-string".startswith(flag):
            rest = words[start:]
            return [attached, *rest] if separator else rest
        return None
    if option == "-" and environment_changes is not None:
        environment_changes.append((None, None))
    for index in range(1, len(option)):
        letter = "-" + option[index]
        if environment_changes is not None:
            if letter == "-i":
                environment_changes.append((None, None))
            elif letter == "-u":
                attached = option[index + 1 :]
                if attached or len(words) > start:
                    environment_changes.append((attached or words[start], None))
        if letter == "-S":
            attached = option[index + 1 :]
            rest = words[start:]
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


def _unwrap(
    words: list[str],
    *,
    stop_at_xargs: bool = False,
    environment_changes: list[tuple[str | None, str | None]] | None = None,
    stop_at_eval: bool = False,
    assignments: set[str] | None = None,
) -> list[str]:
    """Common literal execution wrappers; never treat query operands as launches."""
    i = 0
    # Decoded env -S words run before the untouched argv. A reversed stack
    # avoids rebuilding that argv tail for every separate split-string option.
    pending: list[str] = []

    def remaining() -> int:
        return len(pending) + len(words) - i

    def peek(offset: int = 0) -> str:
        return pending[-1 - offset] if offset < len(pending) else words[i + offset - len(pending)]

    def advance(count: int = 1) -> None:
        nonlocal i
        count = min(count, remaining())
        decoded = min(count, len(pending))
        if decoded:
            del pending[-decoded:]
        i += count - decoded

    def tail() -> list[str]:
        if not pending:
            return words[i:]
        return [*reversed(pending), *words[i:]]

    while remaining():
        name = peek().rsplit("/", 1)[-1]
        if stop_at_xargs and name == "xargs":
            break
        if (
            name in {"for", "select"}
            and remaining() >= 3
            and peek(1).isidentifier()
            and (peek(2) == "do" or (name == "for" and peek(2) == "__gobby_loop_list__"))
        ) or (
            name == "foreach"
            and remaining() >= 3
            and peek(1).isidentifier()
            and peek(2) == "__gobby_loop_list__"
        ):
            advance(3)
        elif _ASSIGNMENT.match(peek()):
            if environment_changes is not None:
                key, _, value = peek().partition("=")
                environment_changes.append((key.removesuffix("+"), value))
            if assignments is not None:
                assignments.add(peek().partition("=")[0].removesuffix("+"))
            advance()
        elif name in {
            "!",
            "{",
            "then",
            "do",
            "else",
            "elif",
            "if",
            "while",
            "until",
            "coproc",
            "noglob",
            "nocorrect",
        }:
            advance()
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
            advance()
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
            while remaining() and peek().startswith("-"):
                option = peek()
                if option == "--":
                    advance()
                    break
                if name == "command" and any(flag in option for flag in "vV"):
                    return []
                if name == "env" and option in {"--help", "--version"}:
                    return []
                following = [peek(1)] if name == "env" and remaining() > 1 else []
                script = (
                    _env_split_string(
                        option, following, takes_value, environment_changes=environment_changes
                    )
                    if name == "env"
                    else None
                )
                if script is not None:
                    if not script:
                        return []
                    # Attached -S strings add a word; separate strings consume
                    # the next argv word. Decode only that source, keeping the
                    # rest of both sources in place.
                    advance(1 if len(script) > len(following) else 2)
                    try:
                        pending.extend(reversed(_split_env_argv(script[0])))
                    except ValueError:
                        return ["sh", "-c", " ".join([script[0], *tail()])]
                    # Split words may themselves contain env options (shebang form).
                    continue
                count = option_word_count(
                    option,
                    takes_value,
                    infer_long_options=True,
                    flag_options=_SUDO_FLAG_OPTIONS if name == "sudo" else (),
                )
                advance(count)
            if name == "timeout" and remaining():
                advance()
        elif name == "eval":
            if stop_at_eval:
                break
            advance()
            return ["sh", "-c", " ".join(tail())]
        else:
            break
    return tail()
