"""Shell syntax masking for bounded executable-context inspection."""

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._case_syntax import CaseSyntax
from gobby.hooks._normalization_shell import (
    ShellToken,
    _skip_heredocs,
    _substitution_end,
    scan_shell_command,
)


def _function_header(tokens: list[ShellToken], index: int) -> tuple[tuple[str, ...], int] | None:
    """Recognize the literal function header forms accepted by Bash and zsh."""
    first = index
    if tokens[index].quoted:
        return None
    keyword = tokens[index].value == "function"
    if keyword:
        index += 1
    names: list[str] = []
    while index < len(tokens) and not tokens[index].quoted:
        word = tokens[index].value
        if word == "()":
            return tuple(names), index
        if word == "{" and keyword:
            return tuple(names), max(first, index - 1)
        attached = word.endswith("()")
        name = word[:-2] if attached else word
        if (
            not name
            or name.isspace()
            or name in {"{", "}", "(", ")", ";", "|", "|&", "&", "&&", "||", ";;", ";&", ";;&"}
        ):
            break
        if any(char in name for char in "$'\"\\="):
            break
        names.append(name)
        if attached:
            return tuple(names), index
        index += 1
    return None


def _group_spans(
    command: str,
) -> tuple[dict[int, int], dict[int, tuple[int, int, int]], dict[int, tuple[tuple[str, ...], int]]]:
    """Match reserved group words only where the shell expects a command.

    Substitutions and heredoc payloads are already opaque scanner tokens.
    Compound bodies retain their control prefixes for recursive inspection.
    """
    scan = scan_shell_command(command)
    braces: dict[int, int] = {}
    compounds: dict[int, tuple[int, int, int]] = {}
    functions: dict[int, tuple[tuple[str, ...], int]] = {}
    fallback_braces: set[int] = set()
    stack: list[tuple[str, int, int]] = []
    position = True
    closing = {
        "if": "fi",
        "while": "done",
        "until": "done",
        "for": "done",
        "select": "done",
        "case": "esac",
        "{": "}",
    }
    skip = -1
    for index, (token, (start, end)) in enumerate(zip(scan.tokens, scan.spans, strict=True)):
        if index <= skip:
            continue
        word = token.value
        if not token.quoted and word in {";", "\n", "|", "|&", "&", "&&", "||", ";;", ";&", ";;&"}:
            position = True
            continue
        if (
            not position
            and not token.quoted
            and word == "{"
            and (start == 0 or command[start - 1].isspace() or command[start - 1] in ";|&(")
            and end < len(command)
            and command[end].isspace()
        ):
            stack.append((word, start, end))
            fallback_braces.add(start)
            position = True
            continue
        if position and not token.quoted:
            header = _function_header(scan.tokens, index)
            if header is not None:
                names, skip = header
                functions[start] = (names, scan.spans[skip][1])
                continue
            if word == "coproc":
                if (
                    index + 2 < len(scan.tokens)
                    and scan.tokens[index + 1].value.isidentifier()
                    and scan.tokens[index + 2].value in {"{", "("}
                ):
                    skip = index + 1
                continue
            if word in closing:
                stack.append((word, start, end))
                position = word not in {"for", "select", "case"}
                continue
            if stack and word == closing[stack[-1][0]]:
                opener, begin, body = stack.pop()
                if opener == "{":
                    braces[begin] = start
                else:
                    compounds[begin] = (body, start, end)
                position = False
                continue
            if word in {"then", "else", "elif", "do", "!", "time"}:
                continue
            # A function header precedes a command-position compound body.
            if word.endswith("()") and word[:-2].isidentifier():
                continue
        position = False
    for opener, start, _ in stack:
        if opener == "{" and start not in fallback_braces:
            braces[start] = -1
    return braces, compounds, functions


def _prepare(command: str, depth: int, *, data: bool = False) -> tuple[str, list[str]]:
    """Mask expansions/comments and separate grouping for the shared shell scanner.

    Heredocs stay untouched for scan_shell_command to associate with their
    consumer. Unquoted data heredocs expand substitutions even inside quotes.
    """
    output: list[str] = []
    expansions: list[str] = []
    quote = ""
    cases = CaseSyntax()
    brace_ends, compounds, functions = ({}, {}, {}) if data else _group_spans(command)
    index = 0
    line_start = 0
    while index < len(command):
        char = command[index]
        pattern_paren = not data and not quote and cases.pattern_paren(command, index, 0)
        if index in functions and not quote:
            names, index = functions[index]
            output.append(" ".join(f"{name}()" for name in names))
            continue
        if index in compounds and not quote:
            body, body_end, end = compounds[index]
            output.append(f"__gobby_group_{len(expansions)}__")
            # Keep the case header so its pattern parentheses remain patterns.
            # Omit the closer so this body is not extracted again recursively.
            expansions.append(
                command[index:body_end]
                if command.startswith(("case", "for", "select"), index)
                else command[body:body_end]
            )
            index = end
            continue
        if (
            not data
            and not quote
            and command[index : index + 2] == "()"
            and index > 0
            and (command[index - 1].isalnum() or command[index - 1] == "_")
        ):
            output.append("()")
            index += 2
            continue
        if char == "\\" and quote != "'":
            output.append(command[index : index + 2])
            index += 2
            continue
        if quote not in {"'", "$"} and (
            command.startswith("$(", index)
            or char == "`"
            or (not data and not quote and command[index : index + 2] in {"<(", ">(", "=("})
        ):
            tick = char == "`"
            start = index + (1 if tick else 2)
            try:
                end = _substitution_end(command, start, tick, depth)
            except ValueError as error:
                if not data or str(error) != "Unclosed shell substitution":
                    raise
                # Inspect incomplete bodies too: data can still contain a launch.
                expansions.append(command[start:])
                output.append("__gobby_expansion__")
                break
            marker = (
                f"__gobby_process_input_{len(expansions)}__"
                if command[index : index + 2] in {"<(", "=("}
                else f"__gobby_process_output_{len(expansions)}__"
                if command.startswith(">(", index)
                else "__gobby_expansion__"
            )
            expansions.append(command[start:end])
            output.append(marker)
            index = end + 1
            continue
        if not data and not quote and ((char == "(" and not pattern_paren) or index in brace_ends):
            if char == "(":
                end = _substitution_end(command, index + 1, False, depth)
            else:
                if brace_ends[index] < 0:
                    raise ValueError("Unclosed shell group")
                end = brace_ends[index]
            output.append(f"__gobby_group_{len(expansions)}__")
            expansions.append(command[index + 1 : end])
            index = end + 1
            continue
        if not data and not quote and opens_ansi_c_quote(command, index):
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
            if command.startswith(">|", index):
                # Noclobber override has the same stream target as ordinary >.
                output.append(">")
                index += 2
                continue
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
