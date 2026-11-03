"""Read targets a Bash or file tool touches, judged by the agent workspace read-scope rule.

Shell words are extracted and expanded (braces, extglobs, dot patterns) without enumerating
the filesystem. A relative operand is anchored to the literal `cd` before it.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from gobby.hooks._normalization_shell import scan_shell_command
from gobby.hooks._path_scope import current_project_root, current_tool_cwd, resolve_tool_path
from gobby.workflows.condition_helpers_paths import workspace_contains

_SHELL_OPERATORS = re.compile(r"[<>|;&()]")
# One lexical unit of shell text. Quoted spans, comments and escapes are consumed whole, so a
# heredoc-looking fragment inside any of them opens nothing.
_SHELL_UNIT = re.compile(
    r"\$'(?:\\.|[^'\\])*'"  # ANSI-C quoted
    r"|'[^']*'"
    r'|"(?:\\.|[^"\\])*"'
    r"|`(?:\\.|[^`\\])*`"
    r"|\\."
    r"|(?<![^\s;&|()])#[^\n]*"  # a comment starts a word
    r"|(?<!<)<<(?P<dash>-?)[ \t]*(?P<quote>['\"])(?P<tag>\w+)(?P=quote)"  # quoted heredoc header
    r"|.",
    re.DOTALL,
)
# A `<name>` segment is documentation placeholder text: seats write only inside their own
# folders, and no folder is named that way.
_PLACEHOLDER_SEGMENT = re.compile(r"<[A-Za-z_][\w-]*>")


def _skip_heredoc_bodies(command: str, pos: int, pending: list[tuple[str, bool]]) -> int:
    """Position just past the last delimiter line, reading the bodies in header order."""
    for tag, strip_tabs in pending:
        while pos < len(command):
            end = command.find("\n", pos)
            line_end = len(command) if end == -1 else end
            line = command[pos:line_end]
            pos = len(command) if end == -1 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                break
    return pos


def _split_heredoc_bodies(command: str) -> tuple[str, list[str]]:
    """Split each quoted-delimiter heredoc body out of the command text.

    The shell reads a quoted-delimiter body as literal text, so it is returned apart from the
    command. The caller judges a body as shell code when a shell reads it, and as text otherwise.
    A body starts after the unquoted newline that ends its header line and runs to the
    delimiter line, matched exactly as the shell matches it.
    """
    kept: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool]] = []
    pos = 0
    while pos < len(command):
        unit = _SHELL_UNIT.match(command, pos)
        if unit is None:  # unreachable: the final alternative matches any character
            return command, []
        text = unit.group(0)
        pos = unit.end()
        if unit.group("tag") is not None:
            pending.append((unit.group("tag"), unit.group("dash") == "-"))
        elif text == "\n" and pending:
            start = pos
            pos = _skip_heredoc_bodies(command, pos, pending)
            bodies.append(command[start:pos])
            pending = []
        kept.append(text)
    return "".join(kept), bodies


_OPERATOR_CHARS = frozenset("();<>|&\n")
_OPERATOR_RUN = re.compile(r"&&|\|\||\|&|[();<>|&\n]")


def _shell_tokens(command: str) -> list[str]:
    """Shell words with operators split off only outside quotes, so quoted text stays whole.

    An unquoted newline ends a command, so it is a token of its own. A backslash-newline
    continues the line, so it is read as a space. shlex joins adjacent operator characters,
    so a run such as `);` is split back into its operators.
    """
    lexer = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    tokens: list[str] = []
    for token in lexer:
        if token and set(token) <= _OPERATOR_CHARS:
            tokens.extend(_OPERATOR_RUN.findall(token))
        else:
            tokens.append(token)
    return tokens


_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# Commands that run their input as shell code, so a quoted heredoc body they read is shell code.
_BODY_CONSUMERS = _SHELL_WRAPPERS | {"eval", "source", "."}
_SHELL_COMMAND_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _wrapped_commands(tokens: list[str]) -> list[str]:
    """Command strings a shell runs with `-c`, or `eval` runs, such as `bash -c "cat <path>"`.

    The flag may follow options that take values, so every later token is searched. Reading
    past the real flag only judges one more word.
    """
    commands: list[str] = []
    for index, token in enumerate(tokens):
        name = os.path.basename(os.path.expandvars(token))
        if name == "eval":
            commands.append(" ".join(tokens[index + 1 :]))
        elif name in _SHELL_WRAPPERS:
            for flag_index in range(index + 1, len(tokens)):
                if _SHELL_COMMAND_FLAG.fullmatch(tokens[flag_index]):
                    commands.extend(tokens[flag_index + 1 : flag_index + 2])
                    break
    return commands


def _path_words(tokens: list[str]) -> list[str]:
    """Words that look like paths. A quoted token holding several words is judged word by word."""
    expanded = (part for token in tokens for part in os.path.expandvars(token).split())
    return [
        part
        for part in expanded
        if "/" in part
        or part.startswith("~")
        or _READ_PATTERN.search(part)
        or _EXTGLOB.search(part)
        or ("{" in part and ("," in part or ".." in part))
    ]


def _mask_regex_operands(command: str) -> str:
    """Mask literal grep/rg regexes and non-executing sed substitutions, keeping paths."""
    scan = scan_shell_command(command)
    head = ""
    seen = False
    expression = False
    value = False
    options = True
    masked = list(command)
    for token, (start, end) in zip(scan.tokens, scan.spans, strict=True):
        word = token.value
        if not token.quoted and word in {"|", "||", "&&", ";", "&", "\n"}:
            head, seen, expression, value, options = "", False, False, False, True
            continue
        if not head:
            head = os.path.basename(word)
            continue
        if head not in {"grep", "egrep", "fgrep", "rg", "sed"}:
            continue
        if value:
            value = False
            continue
        if options and not expression and word == "--":
            options = False
            continue
        if options and not expression and word in {"-e", "--regexp", "--expression"}:
            expression = True
            continue
        if (
            options
            and not expression
            and word in {"-f", "--file", "-g", "--glob", "-A", "-B", "-C", "-m"}
        ):
            value = True
            seen = seen or word in {"-f", "--file"}
            continue
        if options and not expression and word.startswith("-") and word != "-":
            # Unknown or attached options may already supply the pattern. Never
            # mistake the next file for regex data (e.g. -efoo, -nefoo, --file=x).
            if not re.fullmatch(r"-[EFGINPRUabcHhIiLlnoqrsSVvwxz0-9]+", word):
                seen = True
            continue
        pattern = expression or not seen
        seen, expression = True, False
        raw = command[start:end]
        literal = len(raw) >= 2 and (
            (raw[0] == raw[-1] == "'" and "'" not in raw[1:-1])
            or (raw[0] == raw[-1] == '"' and not any(c in raw[1:-1] for c in '$`"'))
        )
        if head == "sed":
            delimiter = re.escape(word[1:2])
            pattern = (
                pattern
                and bool(delimiter)
                and bool(
                    re.fullmatch(
                        rf"s{delimiter}(?:\\.|[^{delimiter}])*{delimiter}"
                        rf"(?:\\.|[^{delimiter}])*{delimiter}[gIp0-9]*",
                        word,
                    )
                )
            )
        if pattern and literal:
            masked[start:end] = " " * (end - start)
    return "".join(masked)


_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&", "(", ")", "\n"})
# Words that start a command. An evaluator there runs its body in the current shell. zsh
# precommand modifiers (`noglob`, `nocorrect`, `exec`, `-`) keep the evaluator at a command start.
_COMMAND_STARTS = _SHELL_SEPARATORS | frozenset(
    {
        "{",
        "!",
        "then",
        "do",
        "else",
        "elif",
        "if",
        "while",
        "until",
        "time",
        "command",
        "builtin",
        "exec",
        "noglob",
        "nocorrect",
        "-",
    }
)
# Commands that move the shell's directory. Their operand is the target. zsh has `chdir`.
_CD_COMMANDS = frozenset({"cd", "chdir", "pushd", "popd"})
# Commands that run a body in the current shell, which may move it. The body is unknown.
_EVALUATORS = frozenset({"eval", "source", ".", "exec", "alias", "trap"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# A command name the hook cannot read literally (expanded, quoted or globbed) may be `cd`.
_DYNAMIC_NAME = re.compile(r"[$`\\'\"\[\]*?{}]")
# zsh `set -J` or `setopt -J` turns on AUTO_CD.
_AUTOCD_FLAG = re.compile(r"[-+][A-Za-z]*J[A-Za-z]*")


def _anchored(words: list[str], directory: Path | None, moved: bool) -> list[str]:
    if not moved or directory is None:
        return words
    return [w if w.startswith(("/", "~")) else os.path.join(directory, w) for w in words]


# A place the shell may be in: a directory, and whether a literal `cd` put the shell there.
# None is a place a dynamic `cd` reached, whose directory cannot be resolved.
_Place = tuple[Path | None, bool] | None
# Marks a read through a place that cannot be resolved. The caller refuses it.
_UNKNOWN_CWD = "\0unknown-cwd"


def _enterable(place: _Place) -> bool:
    """True when `cd` into the place certainly succeeds: an existing, searchable directory."""
    return (
        place is not None
        and place[0] is not None
        and os.path.isdir(place[0])
        and os.access(place[0], os.X_OK)
    )


def _logical_cd_target(arg: str, directory: Path | None) -> Path | None:
    """Where `cd arg` lands on the shell's logical path: `..` folds lexically, links stay."""
    try:
        candidate = Path(arg).expanduser()
    except RuntimeError:
        return None
    if not candidate.is_absolute():
        if directory is None:
            return None
        candidate = directory / candidate
    return Path(os.path.normpath(candidate))


def _redirect_at(tokens: list[str], index: int) -> bool:
    """Whether `tokens[index]` starts a redirection such as `>x` or `2>x`."""
    token = tokens[index]
    following = tokens[index + 1] if index + 1 < len(tokens) else ""
    return token.startswith(("<", ">")) or (token.isdigit() and following.startswith(("<", ">")))


_LIST_JOINS = frozenset({"&&", "||", "|", "|&"})


def _backgrounded(tokens: list[str], start: int) -> bool:
    """Whether the and-or list running through `tokens[start:]` ends at `&`.

    The list ends at a top-level `;` or newline, except a newline after `&&`, `||`, `|` or
    `|&`, which continues it. A `#` comment up to the newline is skipped when finding what
    precedes it, and ends nothing. An `&` anywhere before the end counts, even inside a group
    or what looks like a comment, so a misread nesting or quote only keeps more places.
    """
    depth = 0
    previous = ""
    comment = False
    for token in tokens[start:]:
        if token == "&":
            return True
        if token == "\n" and comment:
            comment = False
        elif comment:
            continue
        elif token.startswith("#"):
            comment = True
            continue
        if token in {"(", "{"}:
            depth += 1
        elif token in {")", "}"}:
            depth -= 1
        elif depth <= 0 and (token == ";" or (token == "\n" and previous not in _LIST_JOINS)):
            return False
        if token != "\n":
            previous = token
    return False


def _place_words(token: str, places: frozenset[_Place]) -> list[str]:
    """Words one token reads from each place. Once a place has moved, every bare word counts."""
    words: list[str] = []
    for place in places:
        if place is None:
            if not token.startswith(("/", "~", "-")):
                words.append(_UNKNOWN_CWD)
            continue
        directory, moved = place
        found = os.path.expandvars(token).split() if moved else _path_words([token])
        words.extend(_anchored(found, directory, moved))
    return words


def _cd_places(arg: str, places: frozenset[_Place], certain: bool) -> frozenset[_Place]:
    """Places the shell may be in after `cd arg` runs from any of `places`.

    A literal target replaces the places only when the cd certainly runs and lands. Otherwise
    the cd may be skipped or fail, so the places before it stay reachable beside its target.
    A dynamic target cannot be resolved, so it adds an unknown place. A literal target lands on
    the logical path, where `..` folds lexically, or on the physical path, where links resolve
    first: bash retries it when the logical path is missing, and `set -P` or zsh `chase_links`
    choose it outright. Both are reachable. Operands are resolved physically when read. A `-N`
    or zsh `+N` target picks from the directory stack, so it is dynamic.
    """
    literal = (
        not arg.startswith(("-", "+")) and "$" not in arg and _READ_PATTERN.search(arg) is None
    )
    landed: set[_Place] = set()
    for place in places:
        directory = place[0] if place is not None else None
        if not literal or (place is None and not arg.startswith(("/", "~"))):
            landed.add(None)
            continue
        for target in (_logical_cd_target(arg, directory), resolve_tool_path(arg, directory)):
            landed.add((target, True) if target is not None else None)
    if certain and all(_enterable(place) for place in landed):
        return frozenset(landed)
    return places | frozenset(landed)


def _cd_anchored_words(
    tokens: list[str], base: Path | None, places: frozenset[_Place]
) -> tuple[list[str], frozenset[_Place]]:
    """Read words anchored to every place the shell may be in when each operand runs.

    `places` is where the shell may be before the command; its hook cwd is `base`. A `cd`,
    `pushd` or `popd` adds its target to those places. Only a leading `cd` whose segment ends
    at `&&`, `;` or a newline replaces them. A subshell restores the places at `)`, and a brace
    group keeps the places before it at `}`. A `CDPATH` in the command makes every target
    dynamic, and a `case` pattern's `)` keeps the places before it, since it is not a close.
    """
    saved: list[tuple[str, frozenset[_Place]]] = []
    skip = -1
    words: list[str] = []
    cdpath = any("CDPATH" in token for token in tokens)
    case_seen = "case" in tokens
    autocd = any("autocd" in token.lower().replace("_", "") for token in tokens) or (
        not {"set", "setopt"}.isdisjoint(tokens)
        and any(_AUTOCD_FLAG.fullmatch(token) for token in tokens)
    )
    command_word = 0
    for index, token in enumerate(tokens):
        if index == skip:
            continue
        previous = tokens[index - 1] if index else ""
        if token in {"(", "{"}:
            saved.append((token, places))
        elif token in {")", "}"}:
            opener, before = saved.pop() if saved else ("", places)
            restore = (opener, token) == ("(", ")") and not case_seen
            places = before if restore else before | places
        elif token.lstrip("`") in _CD_COMMANDS:
            name = token.lstrip("`")
            consumed = index + 1 < len(tokens) and tokens[index + 1] not in _SHELL_SEPARATORS
            raw = tokens[index + 1] if consumed else "~"
            skip = index + 1 if consumed else -1
            words.extend(_place_words(raw, places))
            after = index + 2 if consumed else index + 1
            # zsh `cd old new` swaps `old` for `new` in the current directory.
            swapped = (
                after < len(tokens)
                and tokens[after] not in _SHELL_SEPARATORS
                and not _redirect_at(tokens, after)
            )
            if name == "popd" or cdpath or not consumed or swapped:
                target = "-"
            else:
                target = raw
            # Only a leading `cd` whose segment ends at `&&`, `;` or a newline may narrow, and
            # only when its and-or list is not backgrounded by `&`.
            ends_segment = after == len(tokens) or tokens[after] in {"&&", ";", "\n"}
            certain = (
                name == "cd"
                and index == 0
                and consumed
                and ends_segment
                and not _backgrounded(tokens, after)
            )
            places = _cd_places(target, places, certain)
        elif token not in _SHELL_SEPARATORS:
            words.extend(_place_words(token, places))
            at_start = index in (0, command_word) or previous in _COMMAND_STARTS
            if at_start and _ASSIGNMENT.match(token):
                # A `NAME=value` prefix keeps the next word in command position.
                command_word = index + 1
            elif at_start and (
                token in _EVALUATORS
                or (token not in {"[", "[["} and _DYNAMIC_NAME.search(token) is not None)
                or autocd
            ):
                # An evaluator body or a dynamic command name may move the shell. Under
                # AUTO_CD any command word may name a directory, even a link the command makes.
                places = places | frozenset({None})
            elif at_start:
                # zsh AUTO_CD enters a directory named as a command, like `cd` would.
                entered = _cd_places(token, places, certain=False)
                if any(_enterable(place) for place in entered - places):
                    places = entered
    return words, places


def _command_places_words(command: str, places: frozenset[_Place]) -> list[str]:
    """Judge a nested shell body from every place the shell may be in."""
    words: list[str] = []
    for place in places:
        if place is None:
            words.append(_UNKNOWN_CWD)
        else:
            words.extend(_shell_path_words(command, place[0], place[1]))
    return words


def _shell_path_words(command: Any, base: Path | None = None, moved: bool = False) -> list[str]:
    """Shell words that look like paths: operators split outside quotes, env vars expanded.

    Only known data consumers may omit literal heredoc bodies. Interpreters and
    unknown consumers still need path checks. Shell wrappers and `eval` recurse.
    Relative words are anchored to the literal `cd` before them, starting from `base`.
    """
    if not isinstance(command, str):
        return []
    text, bodies = _split_heredoc_bodies(command)
    text = _mask_regex_operands(text.strip())
    try:
        tokens = _shell_tokens(text)
    except ValueError:
        tokens = _SHELL_OPERATORS.sub(" ", text).split()
    shell_reads = any(os.path.basename(os.path.expandvars(t)) in _BODY_CONSUMERS for t in tokens)
    start = frozenset({(base, moved)})
    words, places = _cd_anchored_words(tokens, base, start)
    # The operator-aware lexer splits extglob parentheses and alternatives. Keep
    # an additional whole-word view so their literal path prefix is not lost.
    try:
        whole_words = shlex.split(text)
    except ValueError:
        whole_words = []
    whole_anchored, _ = _cd_anchored_words(whole_words, base, start)
    words.extend(word for word in whole_anchored if _EXTGLOB.search(word))
    command_name = os.path.basename(os.path.expandvars(tokens[0])) if tokens else ""
    data_consumer = command_name in {"cat", "tee"} or (
        command_name == "git" and tokens[1:2] == ["commit"]
    )
    compound = "\n" in text.strip() or any(
        token in {"|", "||", "&", "&&", ";", "(", ")"} for token in tokens
    )
    if shell_reads or compound or not data_consumer:
        for body in bodies:
            words.extend(_command_places_words(body, places))
    for wrapped in _wrapped_commands(tokens):
        words.extend(_command_places_words(wrapped, places))
    return words


_READ_PATTERN = re.compile(r"[*?\[]")
_EXTGLOB = re.compile(r"[@+?!*]\(")
_BRACE_RANGE = re.compile(r"(?:-?\d+\.\.-?\d+|[a-zA-Z]\.\.[a-zA-Z])(?:\.\.-?\d+)?")
_READ_EXPANSION_LIMIT = 256


def _brace_read_words(raw: str) -> list[str] | None:
    """Expand bounded comma alternatives; approximate ranges with a filename glob.

    Non-expanding braces remain literal, including awk/sed program data. Work on
    complete words before path normalization because alternatives can contain '/'
    or combine separate dot fragments into '..'. None means the bound was exceeded.
    """
    pending = [raw]
    expanded: list[str] = []
    budget = _READ_EXPANSION_LIMIT
    while pending:
        word = pending.pop()
        budget -= 1
        if budget < 0:
            return None
        groups: list[list[int]] = []
        for end, char in enumerate(word):
            if char == "{":
                groups.append([end])
            elif char == "," and groups:
                groups[-1].append(end)
            elif char == "}" and groups:
                positions = groups.pop()
                start = positions[0]
                if len(positions) > 1:
                    bounds = [*positions, end]
                    alternatives = [
                        word[a + 1 : b] for a, b in zip(bounds, bounds[1:], strict=False)
                    ]
                elif _BRACE_RANGE.fullmatch(word[start + 1 : end]):
                    alternatives = ["*"]
                else:
                    continue
                if len(pending) + len(alternatives) > budget:
                    return None
                pending.extend(word[:start] + choice + word[end + 1 :] for choice in alternatives)
                break
        else:
            expanded.append(word)
    return expanded


def _extglob_read_word(word: str) -> str | None:
    """Overapproximate filename extglobs without executing a shell.

    Slash-bearing groups change path structure and cannot be inspected as one
    component. Refuse those rather than silently matching only part of a group.
    """
    while match := _EXTGLOB.search(word):
        depth = 1
        end = match.end()
        while end < len(word) and depth:
            depth += (word[end] == "(") - (word[end] == ")")
            end += 1
        if depth or "/" in word[match.end() : end]:
            return None
        word = word[: match.start()] + "*" + word[end:]
    return word


def _resolve_read_target(raw: Any, base: Path | None) -> tuple[Path, int | None] | None:
    """Resolve a pattern and retain where expansion starts; base paths stay literal."""
    if not isinstance(raw, str) or not raw:
        return None
    parts = Path(os.path.expandvars(os.path.expanduser(raw))).parts
    for index, part in enumerate(parts):
        if _READ_PATTERN.search(part):
            prefix = resolve_tool_path(str(Path(*parts[:index])), base)
            return (
                (prefix.joinpath(*parts[index:]), len(prefix.parts)) if prefix is not None else None
            )
    literal = resolve_tool_path(raw, base)
    return (literal, None) if literal is not None else None


def _shell_dot_read_words(word: str, pattern: str) -> list[str] | None:
    """Include shell dot/parent matches without enumerating filesystem entries."""
    originals = Path(word).parts
    variants = [Path(pattern).parts]
    for index, part in enumerate(originals):
        if not (part.startswith(".") or re.search(r"[|(]\.", part)):
            continue
        for name in (".", ".."):
            if part == name or not _read_pattern_matches(name, variants[0][index]):
                continue
            variants.extend(parts[:index] + (name,) + parts[index + 1 :] for parts in variants[:])
            if len(variants) > _READ_EXPANSION_LIMIT:
                return None
    return [os.path.join(*parts) for parts in variants]


def _workspace_read_targets(
    event_data: Mapping[str, Any] | None, tool_input: Any
) -> list[tuple[Path, int | None]] | None:
    """Resolved paths a tool reads: path-like Bash words, or the path fields of a file tool."""
    if not isinstance(tool_input, Mapping):
        return []
    data = event_data or {}
    base = current_tool_cwd(data) or current_project_root(data)
    raws: list[Any]
    if data.get("tool_name") == "Bash":
        raws = list(_shell_path_words(tool_input.get("command"), base))
        if _UNKNOWN_CWD in raws:
            return None
    else:
        raws = [tool_input.get("file_path"), tool_input.get("path")]
    inputs = [(raw, base, data.get("tool_name") == "Bash") for raw in raws]
    if data.get("tool_name") == "Glob":
        pattern_base = resolve_tool_path(tool_input.get("path"), base) or base
        inputs.append((tool_input.get("pattern"), pattern_base, True))
    paths: list[tuple[Path, int | None]] = []
    for raw, directory, expand in inputs:
        if not isinstance(raw, str) or not raw:
            continue
        # A quoted or structured path can contain literal glob/brace characters.
        # Check that actual filename too, before inspecting possible expansions.
        if (literal := resolve_tool_path(raw, directory)) is not None:
            paths.append((literal, None))
        if not expand:
            continue
        words = _brace_read_words(os.path.expandvars(os.path.expanduser(raw)))
        if words is None:
            return None
        for word in words:
            pattern = _extglob_read_word(word)
            if pattern is None:
                return None
            patterns = (
                _shell_dot_read_words(word, pattern)
                if data.get("tool_name") == "Bash"
                else [pattern]
            )
            if patterns is None:
                return None
            for item in patterns:
                if (path := _resolve_read_target(item, directory)) is not None:
                    paths.append(path)
    return list(dict.fromkeys(paths))


def _read_pattern_matches(name: str, pattern: str) -> bool:
    """Conservatively include shell bracket classes and Unicode/case variants."""
    pattern = re.sub(r"\[.*\]", "*", pattern)
    return fnmatchcase(name, pattern) or fnmatchcase(name.casefold(), pattern.casefold())


def _pattern_reaches_root(parts: tuple[str, ...], root_parts: tuple[str, ...]) -> bool:
    """Whether a pattern has enough matching components to enter the workspace root."""
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    while pending:
        index, depth = pending.pop()
        if depth == len(root_parts):
            return True
        if index == len(parts) or (index, depth) in visited:
            continue
        visited.add((index, depth))
        if parts[index] == "**":
            pending.extend(((index + 1, depth), (index, depth + 1)))
        elif _read_pattern_matches(root_parts[depth], parts[index]):
            pending.append((index + 1, depth + 1))
    return False


def _workspace_read_boundary(target: Path, root: Path, start: int) -> Path | None:
    """Require a literal authorized folder before patterns; never enumerate the fleet.

    An expansion above the workspace can reach other seats, so judge the root itself.
    Fold lexical parents and resolve the literal prefix. Symlinks selected by a
    wildcard require kernel enforcement; #25307 tracks that pending policy decision.
    """
    parts: list[tuple[str, bool]] = []
    for index, part in enumerate(target.parts):
        if part == ".." and len(parts) > 1:
            parts.pop()
        else:
            parts.append((part, index >= start))
    target = Path(*(part for part, _ in parts))
    for index, (part, expandable) in enumerate(parts):
        if not expandable:
            continue
        match = _READ_PATTERN.search(part)
        if match is None:
            continue
        prefix = Path(*target.parts[:index])
        within = workspace_contains(prefix, root)
        if within is None:
            return root
        if within:
            target = prefix
            break
        ancestor = workspace_contains(root, prefix)
        if ancestor is None:
            return root
        if ancestor and _pattern_reaches_root(target.parts[index:], root.parts[index:]):
            return root
        return None
    within = workspace_contains(target, root)
    if within is None:
        return root
    return root.joinpath(*target.parts[len(root.parts) :]) if within else None


def _placeholder_folder(target: Path, root: Path) -> bool:
    """Whether a path names a ``<placeholder>`` folder directly under the workspace root.

    No folder is named that way, so the path is documentation text. A traversal through a
    placeholder still resolves to a real folder and is judged there.
    """
    first = target.relative_to(root).parts[:1]
    return bool(first) and _PLACEHOLDER_SEGMENT.fullmatch(first[0]) is not None
