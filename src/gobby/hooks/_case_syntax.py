"""Raw shell syntax boundaries shared by case consumers."""

from gobby.hooks._ansi_c import opens_ansi_c_quote


def _word_end(text: str, start: int) -> int:
    """Read one shell word without treating quoted syntax as a boundary."""
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
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

    def __init__(self) -> None:
        self.cases: dict[int, list[str]] = {}
        self.command_position: dict[int, bool] = {}
        self.pattern_ends: dict[int, int] = {}
        self.word_end = 0

    def pattern_paren(self, text: str, index: int, level: int) -> bool:
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
        end = _word_end(text, index)
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
        if text[start : _word_end(text, start)] == "esac":
            # The command-position word closes the frame when the caller reaches
            # it; anticipating it here would also close its enclosing case.
            self.cases[level][-1] = "body"
            self.command_position[level] = True
            return
        self.cases[level][-1] = "pattern"
        end = case_pattern_end(text, start, len(text))
        self.pattern_ends[level] = len(text) if end is None else end


def _balanced_parens_end(command: str, index: int) -> int | None:
    """Pair group parentheses, excluding case-pattern syntax."""
    depth = 0
    quote = ""
    cases = CaseSyntax()
    position = index
    while position < len(command):
        char = command[position]
        pattern_paren = not quote and cases.pattern_paren(command, position, depth)
        if char == "\\" and quote != "'":
            position += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(" and not pattern_paren:
            depth += 1
        elif char == ")" and not pattern_paren:
            depth -= 1
            if depth == 0:
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
        elif char == "(":
            contexts.append((closing, quote))
            closing, quote = ")", ""
        elif char == closing and contexts:
            closing, quote = contexts.pop()
        elif char == ")" and not contexts:
            return index
        index += 1
    return None
