"""Raw shell syntax boundaries shared by case consumers."""

from gobby.hooks._ansi_c import opens_ansi_c_quote


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
