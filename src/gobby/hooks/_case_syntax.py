"""Raw shell syntax boundaries shared by case consumers."""

from gobby.hooks._ansi_c import opens_ansi_c_quote


def pid_parameter_end(text: str, index: int) -> int | None:
    """Consume bash's ``$$`` parameter before considering another dollar opener."""
    return index + 2 if text.startswith("$$", index) else None


def _word_expansion_end(text: str, start: int, *, depth: int) -> int | None:
    """Skip one word expansion without exposing its syntax to the outer case."""
    if depth > 24:
        raise ValueError("Shell nesting limit")
    if (end := pid_parameter_end(text, start)) is not None:
        return end
    if text.startswith("$(", start):
        end = _balanced_parens_end(text, start + 1, depth=depth + 1)
        if end is None:
            raise ValueError("Unclosed shell substitution")
        return end
    tick = text[start] == "`"
    if not tick and not text.startswith("${", start):
        return None
    closing = "`" if tick else "}"
    index = start + (1 if tick else 2)
    quote = ""
    while index < len(text):
        char = text[index]
        if char == "\\" and (tick or quote != "'"):
            index += 2
            continue
        if char == closing and (tick or not quote):
            return index + 1
        if quote in {"'", "$'"}:
            if char == "'":
                quote = ""
        elif (
            char in "$`" and (end := _word_expansion_end(text, index, depth=depth + 1)) is not None
        ):
            index = end
            continue
        elif not quote and opens_ansi_c_quote(text, index):
            quote = "$'"
            index += 2
            continue
        elif char in "\"'":
            if not quote:
                quote = char
            elif char == quote:
                quote = ""
        index += 1
    raise ValueError("Unclosed shell word expansion")


def _word_end(text: str, start: int, *, depth: int = 0, backtick: bool = False) -> int:
    """Read one shell word without treating quoted syntax as a boundary."""
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if backtick and char == "`":
            break
        if char in "$`" and quote not in {"'", "$'"}:
            end = _word_expansion_end(text, index, depth=depth)
            if end is not None:
                index = end
                continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char.isspace() or char in "();&|<>":
            break
        index += 1
    return index


class CaseSyntax:
    """Track case headers at each group level, leaving quote handling to callers.

    Pattern parentheses belong to the pattern, including optional leading and
    extglob parentheses. Only command-position reserved words open/close cases.
    The caller feeds unquoted source positions and its current group level.
    """

    def __init__(self, *, depth: int = 0, backtick: bool = False) -> None:
        self.depth = depth
        self.backtick = backtick
        self.cases: dict[int, list[str]] = {}
        self.command_position: dict[int, bool] = {}
        self.pattern_ends: dict[int, int] = {}
        self.word_end = 0

    def pattern_paren(
        self, text: str, index: int, level: int, *, word_end: int | None = None
    ) -> bool:
        """Return whether this position is a pattern parenthesis, not a group."""
        frames = self.cases.setdefault(level, [])
        pattern_end = self.pattern_ends.get(level)
        if pattern_end is not None:
            if index <= pattern_end:
                if index == pattern_end:
                    self.pattern_ends.pop(level)
                    frames[-1] = "body"
                    self.command_position[level] = True
                return text[index] in "()"
        if index < self.word_end:
            return False
        char = text[index]
        if (
            frames
            and frames[-1] == "body"
            and (text.startswith(";;", index) or text.startswith(";&", index))
        ):
            end = index + (3 if text.startswith(";;&", index) else 2)
            self._pattern(text, end, level)
            self.word_end = end
            return False
        if char.isspace() or char in ";&|(){}":
            if char in ";&|\n({}":
                self.command_position[level] = True
            if char == ")":
                self.cases.pop(level, None)
                self.command_position.pop(level, None)
            return False
        # Callers consume expansions themselves. Without an active case or a
        # command-position keyword, their word bounds cannot change case state.
        if not frames:
            if not self.command_position.get(level, True):
                return False
            if not text.startswith(
                ("case", "esac", "if", "then", "else", "elif", "while", "until", "do", "!", "time"),
                index,
            ):
                self.command_position[level] = False
                return False
        end = (
            _word_end(text, index, depth=self.depth, backtick=self.backtick)
            if word_end is None
            else word_end
        )
        if end == index:
            return False
        self.word_end = end
        word = text[index:end]
        if frames and frames[-1] == "subject":
            frames[-1] = "in"
        elif frames and frames[-1] == "in":
            if word == "in":
                self._pattern(text, end, level)
            else:
                frames.pop()
        elif self.command_position.get(level, True):
            if word == "case":
                frames.append("subject")
            elif word == "esac" and frames:
                frames.pop()
            elif word in {"if", "then", "else", "elif", "while", "until", "do", "!", "time"}:
                return False
        self.command_position[level] = False
        return False

    def _pattern(self, text: str, start: int, level: int) -> None:
        while start < len(text) and text[start].isspace():
            start += 1
        if text[start : _word_end(text, start, depth=self.depth, backtick=self.backtick)] == "esac":
            # The command-position word closes the frame when the caller reaches
            # it; anticipating it here would also close its enclosing case.
            self.cases[level][-1] = "body"
            self.command_position[level] = True
            return
        self.cases[level][-1] = "pattern"
        end = case_pattern_end(text, start, len(text))
        self.pattern_ends[level] = len(text) if end is None else end


def _balanced_parens_end(command: str, index: int, *, depth: int = 0) -> int | None:
    """Pair group parentheses, excluding case-pattern syntax."""
    level = 0
    quote = ""
    cases = CaseSyntax(depth=depth)
    position = index
    while position < len(command):
        char = command[position]
        if char in "$`" and quote not in {"'", "$'"}:
            end = _word_expansion_end(command, position, depth=depth)
            if end is not None:
                if not quote:
                    # Use the known expansion end so nested subjects advance
                    # the case frame without rescanning the nested expansion.
                    word_end = _word_end(command, end, depth=depth)
                    cases.pattern_paren(command, position, level, word_end=word_end)
                position = end
                continue
        pattern_paren = not quote and cases.pattern_paren(command, position, level)
        if char == "\\" and quote != "'":
            position += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(" and not pattern_paren:
            level += 1
        elif char == ")" and not pattern_paren:
            level -= 1
            if level == 0:
                return position + 1
        position += 1
    return None


def case_pattern_end(text: str, start: int, end: int) -> int | None:
    """Find the unquoted arm terminator outside word expansions and groups.

    Skip the optional opening parenthesis. Expansion frames preserve their
    enclosing quote context. Returned offsets address the original source,
    including a terminator glued to the arm's first word.
    """
    index = start
    while index < end and text[index].isspace():
        index += 1
    if index < end and text[index] == "(":
        index += 1
    closing = ""
    quote = ""
    contexts: list[tuple[str, str]] = []
    while index < end:
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote in {"'", "$'", "`"}:
            if char == ("'" if quote == "$'" else quote):
                if quote == "`":
                    closing, quote = contexts.pop()
                else:
                    quote = ""
            index += 1
            continue
        if not quote and opens_ansi_c_quote(text, index):
            quote = "$'"
            index += 2
            continue
        if (pid_end := pid_parameter_end(text, index)) is not None:
            index = pid_end
            continue
        if text.startswith("$(", index) or text.startswith("${", index):
            contexts.append((closing, quote))
            closing, quote = (")" if text[index + 1] == "(" else "}"), ""
            index += 2
            continue
        if char == "`":
            contexts.append((closing, quote))
            closing, quote = "`", "`"
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
        elif char == "(" and closing != "}":
            contexts.append((closing, quote))
            closing, quote = ")", ""
        elif char == closing and contexts:
            closing, quote = contexts.pop()
        elif char == ")" and not contexts:
            return index
        index += 1
    return None
