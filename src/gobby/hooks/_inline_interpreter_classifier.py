"""Classification for inline Ruby and Node programs."""

import re
from enum import StrEnum


class _InlineProgramClassification(StrEnum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"
    INDETERMINATE = "indeterminate"


_RUBY_MUTATION = re.compile(
    r"(?:\bFile\s*\.\s*(?:delete|rename|unlink|write)\b"
    r"|\bFileUtils\s*\.\s*\w+\b"
    r"|\bIO\s*\.\s*write\b"
    r"|\bDir\s*\.\s*mkdir\b"
    r"|\bsystem\s*(?:\(|\s))"
)
_RUBY_FILE_OPEN = re.compile(r"\bFile\s*\.\s*open\s*\(")
_RUBY_FILE_OPEN_BARE = re.compile(r"\bFile\s*\.\s*open\b(?!\s*\()(?P<arguments>[^;\n]+)")
_RUBY_DYNAMIC_EXECUTION = re.compile(r"\b(?:eval|instance_eval|class_eval|module_eval|send)\s*\(")
_NODE_FS_MUTATION = re.compile(
    r"(?:\bfs\b|require\s*\(\s*__NODE_FS_MODULE__\s*\))"
    r"(?:\s*\.\s*[A-Za-z_$][\w$]*)*"
    r"\s*\.\s*(?:[A-Za-z_$][\w$]*)?"
    r"(?:write|append|unlink|rm|mkdir|rename|copy)[\w$]*\s*\("
)
_NODE_CHILD_PROCESS = re.compile(r"(?:\bchild_process\b|__NODE_CHILD_PROCESS_MODULE__)")
_NODE_DYNAMIC_EXECUTION = re.compile(
    r"(?:\b(?:eval|Function)\s*\(|\bfs\s*\[|\brequire\s*\((?!\s*__NODE_))"
)


def _quoted_token(value: str, *, language: str) -> tuple[str, bool]:
    if language == "node":
        if value in {"fs", "node:fs"}:
            return "__NODE_FS_MODULE__", False
        if value in {"child_process", "node:child_process"}:
            return "__NODE_CHILD_PROCESS_MODULE__", False
        return "__STRING__", False

    if "#{" in value:
        return "__STRING__", True
    if re.fullmatch(r"[rawbt+]*", value):
        if value.startswith(("w", "a")):
            return "__RUBY_MODE_WRITE__", False
        if value.startswith("r") and "+" not in value:
            return "__RUBY_MODE_READ__", False
    return "__STRING__", False


def _code_tokens(source: str, *, language: str) -> tuple[str, bool]:
    """Remove comments and string bodies while retaining security-relevant literals."""
    output: list[str] = []
    indeterminate = False
    index = 0
    while index < len(source):
        if language == "ruby" and source[index] == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if language == "node" and source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if language == "node" and source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                return "".join(output), True
            index = end + 2
            continue

        quote = source[index]
        if quote not in {"'", '"', "`"}:
            output.append(quote)
            index += 1
            continue
        if language == "ruby" and quote == "`":
            output.append("__RUBY_BACKTICK__")
        index += 1
        value: list[str] = []
        while index < len(source) and source[index] != quote:
            if source[index] == "\\" and index + 1 < len(source):
                value.append(source[index + 1])
                index += 2
            else:
                value.append(source[index])
                index += 1
        if index >= len(source):
            return "".join(output), True
        index += 1
        if language == "node" and quote == "`" and "${" in "".join(value):
            indeterminate = True
        if not (language == "ruby" and quote == "`"):
            token, token_indeterminate = _quoted_token("".join(value), language=language)
            output.append(token)
            indeterminate = indeterminate or token_indeterminate
    return "".join(output), indeterminate


def _parenthesized_arguments(code: str, start: int) -> list[str] | None:
    arguments: list[str] = []
    argument_start = start
    depth = 1
    for index in range(start, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                arguments.append(code[argument_start:index])
                return arguments
        elif code[index] == "," and depth == 1:
            arguments.append(code[argument_start:index])
            argument_start = index + 1
    return None


def _ruby_open_mode_classification(arguments: list[str]) -> _InlineProgramClassification | None:
    if len(arguments) < 2:
        return None
    mode = arguments[1]
    if "__RUBY_MODE_WRITE__" in mode:
        return _InlineProgramClassification.MUTATION
    if "__RUBY_MODE_READ__" not in mode:
        return _InlineProgramClassification.INDETERMINATE
    return None


def _ruby_file_open_classification(code: str) -> _InlineProgramClassification | None:
    for match in _RUBY_FILE_OPEN.finditer(code):
        arguments = _parenthesized_arguments(code, match.end())
        if arguments is None:
            return _InlineProgramClassification.INDETERMINATE
        classification = _ruby_open_mode_classification(arguments)
        if classification is not None:
            return classification
    for match in _RUBY_FILE_OPEN_BARE.finditer(code):
        arguments = match.group("arguments").split(",")
        if len(arguments) == 1:
            continue
        classification = _ruby_open_mode_classification(arguments)
        if classification is not None:
            return classification
    return None


def _classify_interpreter_source(
    interpreter: str,
    source: str,
) -> _InlineProgramClassification:
    if interpreter == "ruby":
        code, indeterminate = _code_tokens(source, language="ruby")
        if "__RUBY_BACKTICK__" in code or _RUBY_MUTATION.search(code):
            return _InlineProgramClassification.MUTATION
        open_classification = _ruby_file_open_classification(code)
        if open_classification is not None:
            return open_classification
        if indeterminate or _RUBY_DYNAMIC_EXECUTION.search(code):
            return _InlineProgramClassification.INDETERMINATE
        return _InlineProgramClassification.READ_ONLY

    if interpreter == "node":
        code, indeterminate = _code_tokens(source, language="node")
        if _NODE_CHILD_PROCESS.search(code) or _NODE_FS_MUTATION.search(code):
            return _InlineProgramClassification.MUTATION
        if indeterminate or _NODE_DYNAMIC_EXECUTION.search(code):
            return _InlineProgramClassification.INDETERMINATE
        return _InlineProgramClassification.READ_ONLY

    return _InlineProgramClassification.INDETERMINATE


def _classify_inline_interpreter(
    interpreter: str,
    arguments: list[str],
) -> _InlineProgramClassification:
    flags = {"-e", "--eval"} if interpreter == "node" else {"-e"}
    for index, argument in enumerate(arguments):
        if argument in flags:
            if index + 1 >= len(arguments):
                return _InlineProgramClassification.INDETERMINATE
            return _classify_interpreter_source(interpreter, arguments[index + 1])
        if interpreter == "node" and argument.startswith("--eval="):
            return _classify_interpreter_source(interpreter, argument.partition("=")[2])
    return _InlineProgramClassification.INDETERMINATE
