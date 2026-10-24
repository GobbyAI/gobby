"""Validation of definition-supplied regular expression lengths."""

import ast
import io
import tokenize
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from jinja2 import Environment, TemplateSyntaxError, nodes

from gobby.config.app import DaemonConfig


@dataclass(frozen=True)
class DefinitionRegex:
    field: str
    pattern: str


def iter_definition_regexes(data: object, field: str = "") -> Iterator[DefinitionRegex]:
    """Enumerate regex inputs in a definition."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            path = f"{field}.{key}" if field else str(key)
            if isinstance(key, str) and (
                key in {"pattern", "regex", "line_regex"} or key.endswith(("_pattern", "_regex"))
            ):
                values = value if isinstance(value, list | tuple) else [value]
                for index, pattern in enumerate(values):
                    if isinstance(pattern, str):
                        location = f"{path}[{index}]" if isinstance(value, list | tuple) else path
                        yield DefinitionRegex(location, pattern)
            else:
                yield from iter_definition_regexes(value, path)
    elif isinstance(data, list | tuple):
        for index, value in enumerate(data):
            yield from iter_definition_regexes(value, f"{field}[{index}]")
    elif isinstance(data, str):
        yield from _condition_regexes(data, field)
        if "regex_search" in data or "regex_replace" in data:
            try:
                template = Environment().parse(data)
            except TemplateSyntaxError:
                # Prose containing a filter example is not necessarily a Jinja template.
                return
            for node in template.find_all(nodes.Filter):
                if node.name not in {"regex_search", "regex_replace"}:
                    continue
                argument = (
                    node.args[0]
                    if node.args
                    else next((kw.value for kw in node.kwargs if kw.key == "pattern"), None)
                )
                if isinstance(argument, nodes.Const) and isinstance(argument.value, str):
                    yield DefinitionRegex(field, argument.value)


def _condition_regexes(expression: str, field: str) -> Iterator[DefinitionRegex]:
    if "write_paths_match" not in expression and "assistant_response_matches_any" not in expression:
        return
    try:
        tokens = tokenize.generate_tokens(io.StringIO(expression).readline)
        normalized = tokenize.untokenize(
            (token.type, token.string)
            for token in tokens
            if token.type not in {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL}
        )
        tree = ast.parse(normalized.strip(), mode="eval")
    except (SyntaxError, tokenize.TokenError):
        return
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id == "write_paths_match":
            argument = (
                call.args[2]
                if len(call.args) > 2
                else next((kw.value for kw in call.keywords if kw.arg == "pattern"), None)
            )
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield DefinitionRegex(field, argument.value)
        elif call.func.id == "assistant_response_matches_any":
            regex = (
                call.args[1]
                if len(call.args) > 1
                else next((kw.value for kw in call.keywords if kw.arg == "regex"), None)
            )
            if not (
                isinstance(regex, ast.Constant)
                and regex.value is True
                or isinstance(regex, ast.Name)
                and regex.id == "true"
            ):
                continue
            argument = (
                call.args[0]
                if call.args
                else next((kw.value for kw in call.keywords if kw.arg == "patterns"), None)
            )
            if isinstance(argument, ast.List | ast.Tuple):
                for item in argument.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        yield DefinitionRegex(field, item.value)


def validate_definition_regexes(
    name: str, data: Mapping[str, object], config: DaemonConfig
) -> None:
    """Reject overlong definition regexes using daemon configuration."""
    cap = config.workflow.definition_regex_max_length
    for regex in iter_definition_regexes(data):
        length = len(regex.pattern)
        if length > cap:
            raise ValueError(
                f"Definition {name!r} field {regex.field!r}: regex length {length} "
                f"exceeds configured cap {cap}"
            )
