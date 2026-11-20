"""Regex audit: enumeration of the definition universe, the lexer and the report writer.

The universe is every regex the production enumerator finds in the bundled definition
directories, plus the production contrastive constants. Each found pattern is attributed to the
consumer class whose production call runs it, and the class decides the Python engine, the
external flags and the driver mode the audit uses for it.

Contracts the tests rely on: a class Node's ``detail`` holds ``items`` (its member atom texts)
and ``negated``; ``ACCEPTED_DIVERGENCES`` is read through this module at call time, so a test
can patch it; the JSON report holds ``rows`` and ``decisions`` (each with ``id``, ``python``
keyed by minor, and ``rust``).
"""

from __future__ import annotations

import ast
import importlib
import io
import tokenize
import tomllib
import unicodedata
import warnings
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from jinja2 import Environment, TemplateSyntaxError, nodes

from gobby.workflows.assistant_response_matching import ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS
from gobby.workflows.regex_validation import iter_definition_regexes

if TYPE_CHECKING:
    from tests.contracts.regex_audit_check import DomainTable, Verdict

REPO = Path(__file__).resolve().parents[2]
UNIVERSE_ROOTS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (REPO / "src" / "gobby" / "install" / "shared", ("*.yaml", "*.toml")),
    (REPO / ".gobby" / "workflows" / "rules", ("*.yaml",)),
)
PYTHON_CONSTANT_SOURCE = (
    "python:gobby.workflows.assistant_response_matching.ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS"
)

CONSUMER_CLASSES: tuple[str, ...] = (
    "BOOL",
    "INERT-EXEMPTION",
    "FULLMATCH",
    "MATCH-GROUPS-FIRSTLINE",
    "SEARCH-PKG-M",
    "SPAN-ALLOWLIST",
    "FULLMATCH-PATH",
    "SEARCH-GROUP",
    "SUB",
    "UNKNOWN",
)

# How a found pattern reaches its class: a key the production enumerator yields, a condition call
# or a template filter that holds the literal, or the production constant. A key not listed here
# is UNKNOWN; a command_not_pattern with no truthy sibling command_pattern is INERT-EXEMPTION.
CONSUMERS: Mapping[str, str] = {
    "command_pattern": "BOOL",
    "command_not_pattern": "BOOL",
    "value_pattern": "FULLMATCH",
    "consensus_pattern": "MATCH-GROUPS-FIRSTLINE",
    "line_regex": "SEARCH-PKG-M",
    "condition:assistant_response_matches_any": "SPAN-ALLOWLIST",
    "condition:write_paths_match": "FULLMATCH-PATH",
    "filter:regex_search": "SEARCH-GROUP",
    "filter:regex_replace": "SUB",
    "constant": "SPAN-ALLOWLIST",
}

SUBJECT_DOMAINS: Mapping[str, str] = {
    consumer: ("scalar-first-line" if consumer == "MATCH-GROUPS-FIRSTLINE" else "scalar")
    for consumer in CONSUMER_CLASSES
}
CONSUMER_ENGINES: Mapping[str, Literal["re", "regex"]] = {
    consumer: ("regex" if consumer == "SEARCH-PKG-M" else "re") for consumer in CONSUMER_CLASSES
}
CONSUMER_FLAGS: Mapping[str, frozenset[str]] = dict.fromkeys(CONSUMER_CLASSES, frozenset()) | {
    "SPAN-ALLOWLIST": frozenset({"case_insensitive"}),
    "SEARCH-PKG-M": frozenset({"multi_line"}),
}
Mode = Literal["search", "match", "fullmatch", "sub"]
_DEFAULT_MODE: Mode = "search"
CONSUMER_MODES: Mapping[str, Mode] = dict.fromkeys(CONSUMER_CLASSES, _DEFAULT_MODE) | {
    "FULLMATCH": "fullmatch",
    "FULLMATCH-PATH": "fullmatch",
    "MATCH-GROUPS-FIRSTLINE": "match",
    "SUB": "sub",
}
BOUNDARY_SUBJECTS: Mapping[str, tuple[str, ...]] = {
    "empty": ("",),
    "nonword_only": ("!", "!!", " "),
    "word_edge": ("a", "a!", "!a", "ab"),
}
IGNORE_CASE_LITERALS: tuple[int, int] = (0x00, 0x7F)
MEMBERSHIP_BASIS: Mapping[str, object] = {
    "shorthand": ("\\s", "\\S", "\\d", "\\D", "\\w", "\\W"),
    "ignore_case_literals": IGNORE_CASE_LITERALS,
}
SUB_REPLACEMENT = "n"
DOMAIN_TRANSLATION_MAX_RANGES = 64
ACCEPTED_DIVERGENCES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FoundRegex:
    source: str
    location: str
    pattern: str
    consumer: str
    via: str
    reference: bool = False


@dataclass(frozen=True)
class Node:
    kind: str
    span: tuple[int, int]
    detail: Mapping[str, object]
    children: tuple[Node, ...]


class UnknownConstruct(ValueError):
    """A construct the lexer does not know; the message is the construct's text."""


def class_members(node: Node) -> tuple[tuple[Any, ...], ...]:
    """A class node's members as the lexer stores them: (kind, value, ...) tuples."""
    return tuple(cast(Sequence[tuple[Any, ...]], node.detail["members"]))


def load_documents(path: Path) -> list[object]:
    """The documents of one definition file, parsed as production parses its format."""
    text = path.read_text()
    if path.suffix == ".toml":
        return [tomllib.loads(text)]
    return list(yaml.safe_load_all(text))


def _key_matches(key: object) -> bool:
    return isinstance(key, str) and (
        key in {"pattern", "regex", "line_regex"} or key.endswith(("_pattern", "_regex"))
    )


def _call_argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def _condition_literals(expression: str) -> Iterator[tuple[str, str]]:
    """(via, pattern) for each literal the production condition scan yields, in its order."""
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
            argument = _call_argument(call, 2, "pattern")
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield "condition:write_paths_match", argument.value
        elif call.func.id == "assistant_response_matches_any":
            switch = _call_argument(call, 1, "regex")
            truthy = (isinstance(switch, ast.Constant) and switch.value is True) or (
                isinstance(switch, ast.Name) and switch.id == "true"
            )
            argument = _call_argument(call, 0, "patterns")
            if truthy and isinstance(argument, ast.List | ast.Tuple):
                for element in argument.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        yield "condition:assistant_response_matches_any", element.value


def _filter_literals(text: str) -> Iterator[tuple[str, str]]:
    if "regex_search" not in text and "regex_replace" not in text:
        return
    try:
        template = Environment().parse(text)
    except TemplateSyntaxError:
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
            yield f"filter:{node.name}", argument.value


def _key_consumer(key: str, mapping: Mapping[object, object]) -> str:
    if key == "command_not_pattern" and not mapping.get("command_pattern"):
        return "INERT-EXEMPTION"
    return CONSUMERS.get(key, "UNKNOWN")


def _attributed(data: object, path: str = "") -> Iterator[tuple[str, str, str, str]]:
    """(location, consumer, via, pattern) per found pattern, in the production order."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            location = f"{path}.{key}" if path else str(key)
            if _key_matches(key):
                values = value if isinstance(value, list | tuple) else [value]
                for index, pattern in enumerate(values):
                    if isinstance(pattern, str):
                        listed = isinstance(value, list | tuple)
                        where = f"{location}[{index}]" if listed else location
                        yield where, _key_consumer(str(key), data), "key", pattern
            else:
                yield from _attributed(value, location)
    elif isinstance(data, list | tuple):
        for index, value in enumerate(data):
            yield from _attributed(value, f"{path}[{index}]")
    elif isinstance(data, str):
        for via, pattern in (*_condition_literals(data), *_filter_literals(data)):
            yield path, CONSUMERS[via], via, pattern


def enumerate_regexes(data: object, source: str) -> list[FoundRegex]:
    """Every regex the production enumerator finds in ``data``, attributed to its consumer."""
    found = [
        FoundRegex(source, location, pattern, consumer, via)
        for location, consumer, via, pattern in _attributed(data)
    ]
    production = Counter((r.field, r.pattern) for r in iter_definition_regexes(data))
    attributed = Counter((item.location, item.pattern) for item in found)
    if production != attributed:
        missing = sorted((production - attributed).elements())
        extra = sorted((attributed - production).elements())
        raise ValueError(f"attribution differs from production: missing {missing}, extra {extra}")
    return found


def scan_universe(
    roots: Sequence[tuple[Path, tuple[str, ...]]] = UNIVERSE_ROOTS,
    repo: Path = REPO,
    include_constants: bool = True,
) -> list[FoundRegex]:
    """Every found regex under ``roots`` plus, when asked, the production constants."""
    found: list[FoundRegex] = []
    for root, globs in roots:
        for glob in globs:
            for path in sorted(root.rglob(glob)):
                source = path.relative_to(repo).as_posix()
                for document in load_documents(path):
                    found.extend(enumerate_regexes(document, source))
    if not include_constants:
        return found
    constants = tuple(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS)
    by_constant = {pattern: index for index, pattern in enumerate(constants)}
    resolved: list[FoundRegex] = []
    for item in found:
        if item.consumer == "SPAN-ALLOWLIST" and item.pattern in by_constant:
            item = FoundRegex(
                item.source, item.location, item.pattern, item.consumer, item.via, True
            )
        resolved.append(item)
    for index, pattern in enumerate(constants):
        resolved.append(
            FoundRegex(PYTHON_CONSTANT_SOURCE, f"[{index}]", pattern, "SPAN-ALLOWLIST", "constant")
        )
    return resolved


# ---------------------------------------------------------------------------------------------
# The lexer: Python `re` syntax as a tree of spans. A pattern that is one atom lexes to that atom.
# ---------------------------------------------------------------------------------------------

SHORTHAND_CONSTRUCTS: Mapping[str, str] = {
    "s": "space-shorthand",
    "S": "not-space-shorthand",
    "d": "digit-shorthand",
    "D": "not-digit-shorthand",
    "w": "word-shorthand",
    "W": "not-word-shorthand",
}
# The shorthands whose set holds a newline.
_NEWLINE_SHORTHANDS = frozenset({"\\s", "\\W", "\\D"})
_CONTROL_ESCAPES = {"n": 10, "t": 9, "r": 13, "f": 12, "v": 11, "a": 7}
_FLAG_LETTERS = frozenset("aiLmsux")
_END = "<end>"


def _literal_text(point: int) -> str:
    """A standalone pattern text, valid in Python and Rust, that matches exactly ``point``."""
    character = chr(point)
    if character.isascii() and (character.isalnum() or character == "_"):
        return character
    if point < 0x100:
        return f"\\x{point:02x}"
    if point < 0x10000:
        return f"\\u{point:04x}"
    return f"\\U{point:08x}"


class _Lexer:
    def __init__(self, pattern: str) -> None:
        self.text = pattern
        self.pos = 0
        self.groups = 0
        self.widths: dict[int, int] = {}
        self.names: dict[str, int] = {}
        self.global_flags: set[str] = set()

    def error(self, construct: str) -> UnknownConstruct:
        return UnknownConstruct(construct)

    def peek(self, offset: int = 0) -> str:
        """The character at ``pos + offset``, or ``_END``, which no character test accepts."""
        index = self.pos + offset
        return self.text[index] if index < len(self.text) else _END

    def skip_verbose(self, flags: frozenset[str]) -> None:
        if "x" not in flags:
            return
        while self.pos < len(self.text):
            if self.peek() in " \t\n\r\f\v":
                self.pos += 1
            elif self.peek() == "#":
                while self.pos < len(self.text) and self.peek() != "\n":
                    self.pos += 1
            else:
                return

    def leading_flags(self) -> None:
        while self.text.startswith("(?", self.pos):
            end = self.text.find(")", self.pos)
            letters = self.text[self.pos + 2 : end] if end > 0 else ""
            if not letters or not set(letters) <= _FLAG_LETTERS:
                return
            self.global_flags |= set(letters)
            self.pos = end + 1

    def parse(self) -> Node:
        self.leading_flags()
        flags = frozenset(self.global_flags)
        body = self.alternation(flags)
        if self.pos != len(self.text):
            raise self.error(self.text[self.pos :])
        return body

    def alternation(self, flags: frozenset[str]) -> Node:
        start = self.pos
        branches = [self.sequence(flags)]
        while self.peek() == "|":
            self.pos += 1
            branches.append(self.sequence(flags))
        if len(branches) == 1:
            return branches[0]
        return Node("alt", (start, self.pos), {}, tuple(branches))

    def sequence(self, flags: frozenset[str]) -> Node:
        start = self.pos
        items: list[Node] = []
        while True:
            self.skip_verbose(flags)
            if self.pos >= len(self.text) or self.peek() in "|)":
                break
            items.append(self.quantified(self.atom(flags), flags))
        return Node("seq", (start, self.pos), {}, tuple(items))

    def quantifier(self) -> tuple[int, int | None] | None:
        character = self.peek()
        if character in "*+?":
            self.pos += 1
            return {"*": (0, None), "+": (1, None), "?": (0, 1)}[character]
        if character != "{":
            return None
        end = self.text.find("}", self.pos)
        body = self.text[self.pos + 1 : end] if end > 0 else ""
        low, comma, high = body.partition(",")
        if not body or not (low.isdigit() or (comma and not low)):
            return None
        if comma and high and not high.isdigit():
            return None
        if not comma and not low.isdigit():
            return None
        self.pos = end + 1
        minimum = int(low) if low else 0
        maximum = (int(high) if high else None) if comma else minimum
        return minimum, maximum

    def quantified(self, item: Node, flags: frozenset[str]) -> Node:
        while True:
            self.skip_verbose(flags)
            start = item.span[0]
            bounds = self.quantifier()
            if bounds is None:
                return item
            lazy = possessive = False
            if self.peek() == "?":
                lazy = True
                self.pos += 1
            elif self.peek() == "+":
                possessive = True
                self.pos += 1
            detail = {"min": bounds[0], "max": bounds[1], "lazy": lazy, "possessive": possessive}
            item = Node("repeat", (start, self.pos), detail, (item,))

    def atom(self, flags: frozenset[str]) -> Node:
        start = self.pos
        character = self.peek()
        ci = "i" in flags
        if character == "(":
            return self.group(flags)
        if character == "[":
            return self.character_class(flags)
        if character == "\\":
            return self.escape(flags)
        self.pos += 1
        if character == ".":
            return Node("any", (start, self.pos), {"dotall": "s" in flags}, ())
        if character in "^$":
            detail = {"kind": character, "multiline": "m" in flags, "ci": ci}
            return Node("anchor", (start, self.pos), detail, ())
        if character in "*+?":
            raise self.error(character)
        return self.literal(start, ord(character), ci)

    def literal(self, start: int, point: int, ci: bool) -> Node:
        detail = {"cp": point, "ci": ci, "text": self.text[start : self.pos]}
        return Node("literal", (start, self.pos), detail, ())

    def escaped_point(self, in_class: bool) -> int | str:
        """The code point of the escape at ``pos`` (after the backslash), or a shorthand text."""
        character = self.peek()
        self.pos += 1
        if character in SHORTHAND_CONSTRUCTS:
            return "\\" + character
        if character in _CONTROL_ESCAPES:
            return _CONTROL_ESCAPES[character]
        if character in "xuU":
            size = {"x": 2, "u": 4, "U": 8}[character]
            digits = self.text[self.pos : self.pos + size]
            if len(digits) != size or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise self.error("\\" + character + digits)
            self.pos += size
            return int(digits, 16)
        if character == "N":
            end = self.text.find("}", self.pos)
            if self.peek() != "{" or end < 0:
                raise self.error("\\N")
            name = self.text[self.pos + 1 : end]
            self.pos = end + 1
            return ord(unicodedata.lookup(name))
        if character == "0" or (
            character in "1234567" and (in_class or self.text[self.pos : self.pos + 2].isdigit())
        ):
            digits = character
            while len(digits) < 3 and self.peek() in "01234567":
                digits += self.peek()
                self.pos += 1
            return int(digits, 8)
        if character == "b" and in_class:
            return 8
        if character == _END or (character.isascii() and character.isalnum()):
            raise self.error("\\" + character)
        return ord(character)

    def escape(self, flags: frozenset[str]) -> Node:
        start = self.pos
        self.pos += 1
        character = self.peek()
        ci = "i" in flags
        if character in "bBAZz":
            self.pos += 1
            detail = {"kind": "\\" + character, "multiline": "m" in flags, "ci": ci}
            return Node("anchor", (start, self.pos), detail, ())
        if character in "123456789" and not self.text[self.pos + 1 : self.pos + 3].isdigit():
            digits = character
            self.pos += 1
            if self.peek().isdigit():
                digits += self.peek()
                self.pos += 1
            group = int(digits)
            detail = {"group": group, "width": self.widths.get(group, 0), "named": False}
            return Node("backref", (start, self.pos), detail, ())
        if character == "k" and self.peek(1) == "<":
            end = self.text.find(">", self.pos)
            name = self.text[self.pos + 2 : end]
            if end < 0 or name not in self.names:
                raise self.error("\\k<")
            self.pos = end + 1
            group = self.names[name]
            detail = {"group": group, "width": self.widths.get(group, 0), "named": True}
            return Node("backref", (start, self.pos), detail, ())
        if character in "pP" and self.peek(1) == "{":
            # A Unicode property; neither supported engine of the audit reads it the same way.
            end = self.text.find("}", self.pos)
            if end < 0:
                raise self.error("\\" + character)
            self.pos = end + 1
            detail = {"cp": -1, "ci": ci, "text": self.text[start : self.pos]}
            return Node("literal", (start, self.pos), detail, ())
        value = self.escaped_point(in_class=False)
        if isinstance(value, str):
            return Node("shorthand", (start, self.pos), {"text": value, "ci": ci}, ())
        return self.literal(start, value, ci)

    def group(self, flags: frozenset[str]) -> Node:
        start = self.pos
        self.pos += 1
        if self.peek() != "?":
            self.groups += 1
            index = self.groups
            body = self.alternation(flags)
            self.close(start)
            self.widths[index] = min_width(body)
            detail = {"capture": True, "index": index}
            return Node("group", (start, self.pos), detail, (body,))
        self.pos += 1
        head = self.peek()
        if head in "=!":
            self.pos += 1
            return self.look(start, flags, ahead=True, negative=head == "!")
        if head == "<" and self.peek(1) in "=!":
            negative = self.peek(1) == "!"
            self.pos += 2
            return self.look(start, flags, ahead=False, negative=negative)
        if head == ">":
            self.pos += 1
            body = self.alternation(flags)
            self.close(start)
            return Node("atomic", (start, self.pos), {}, (body,))
        if head == "#":
            end = self.text.find(")", self.pos)
            if end < 0:
                raise self.error("(?#")
            self.pos = end + 1
            return Node("seq", (start, self.pos), {"comment": True}, ())
        if head == "P" or (head == "<" and (self.peek(1).isalpha() or self.peek(1) == "_")):
            # The angle form `(?<name>...)` is lexed like `(?P<name>...)` so that classify can
            # report it from the compile step; Python's re rejects it.
            if head == "<":
                self.pos -= 1
            return self.named(start, flags)
        if head == "(":
            return self.conditional(start, flags)
        return self.scoped(start, flags)

    def close(self, start: int) -> None:
        if self.peek() != ")":
            raise self.error(self.text[start:])
        self.pos += 1

    def look(self, start: int, flags: frozenset[str], *, ahead: bool, negative: bool) -> Node:
        body = self.alternation(flags)
        self.close(start)
        detail = {"ahead": ahead, "negative": negative}
        return Node("look", (start, self.pos), detail, (body,))

    def named(self, start: int, flags: frozenset[str]) -> Node:
        self.pos += 1
        if self.peek() == "<":
            end = self.text.find(">", self.pos)
            name = self.text[self.pos + 1 : end]
            if end < 0 or not name.isidentifier():
                raise self.error("(?P<")
            self.pos = end + 1
            self.groups += 1
            index = self.groups
            self.names[name] = index
            body = self.alternation(flags)
            self.close(start)
            self.widths[index] = min_width(body)
            detail = {"capture": True, "index": index, "name": name}
            return Node("group", (start, self.pos), detail, (body,))
        if self.peek() == "=":
            end = self.text.find(")", self.pos)
            name = self.text[self.pos + 1 : end]
            if end < 0 or name not in self.names:
                raise self.error("(?P=")
            self.pos = end + 1
            group = self.names[name]
            detail = {"group": group, "width": self.widths.get(group, 0), "named": True}
            return Node("backref", (start, self.pos), detail, ())
        raise self.error("(?P")

    def conditional(self, start: int, flags: frozenset[str]) -> Node:
        end = self.text.find(")", self.pos)
        if end < 0:
            raise self.error("(?(")
        self.pos = end + 1
        yes = self.sequence(flags)
        branches = [yes]
        if self.peek() == "|":
            self.pos += 1
            branches.append(self.sequence(flags))
        self.close(start)
        return Node("conditional", (start, self.pos), {"two": len(branches) == 2}, tuple(branches))

    def scoped(self, start: int, flags: frozenset[str]) -> Node:
        add = ""
        while self.peek() in _FLAG_LETTERS:
            add += self.peek()
            self.pos += 1
        remove = ""
        if self.peek() == "-":
            self.pos += 1
            while self.peek() in _FLAG_LETTERS:
                remove += self.peek()
                self.pos += 1
        if self.peek() != ":":
            raise self.error(self.text[start : self.pos + 1])
        self.pos += 1
        inner = frozenset((set(flags) | set(add)) - set(remove))
        body = self.alternation(inner)
        self.close(start)
        detail = {"capture": False, "add": add, "remove": remove}
        return Node("group", (start, self.pos), detail, (body,))

    def class_member(self) -> tuple[str, object, str]:
        """(kind, value, raw text) of one class member: a code point or a shorthand."""
        start = self.pos
        if self.peek() == "\\":
            self.pos += 1
            value = self.escaped_point(in_class=True)
            if isinstance(value, str):
                return "shorthand", value, self.text[start : self.pos]
            return "point", value, self.text[start : self.pos]
        if self.peek() == _END:
            raise self.error("[")
        self.pos += 1
        return "point", ord(self.text[start]), self.text[start : self.pos]

    def character_class(self, flags: frozenset[str]) -> Node:
        start = self.pos
        self.pos += 1
        negated = self.peek() == "^"
        if negated:
            self.pos += 1
        members: list[tuple[object, ...]] = []
        first = True
        while first or self.peek() != "]":
            if self.peek() == _END:
                raise self.error(self.text[start:])
            first = False
            kind, value, raw = self.class_member()
            if kind == "shorthand":
                members.append(("shorthand", value, raw))
                continue
            if self.peek() == "-" and self.peek(1) not in ("]", _END):
                self.pos += 1
                high_kind, high, high_raw = self.class_member()
                if high_kind == "shorthand":
                    raise self.error(self.text[start : self.pos])
                members.append(("range", value, high, raw, high_raw))
            else:
                members.append(("range", value, value, raw, raw))
        self.pos += 1
        ci = "i" in flags
        items = tuple(_member_item(member, ci) for member in members)
        detail = {"items": items, "negated": negated, "ci": ci, "members": tuple(members)}
        return Node("class", (start, self.pos), detail, ())


def _member_item(member: tuple[object, ...], ci: bool) -> str:
    if member[0] == "shorthand":
        text = str(member[1])
    else:
        low, high = int(str(member[1])), int(str(member[2]))
        text = (
            _literal_text(low) if low == high else f"[{_literal_text(low)}-{_literal_text(high)}]"
        )
    return f"(?i:{text})" if ci else text


def _unwrap(node: Node) -> Node:
    """A pattern of one atom, alone or inside a non-capturing group, lexes to that atom."""
    while True:
        if node.kind == "seq" and len(node.children) == 1 and not node.detail:
            node = node.children[0]
        elif node.kind == "group" and not node.detail.get("capture"):
            node = node.children[0]
        else:
            return node


def lex(pattern: str) -> Node:
    """The lexer tree of ``pattern``; raises UnknownConstruct for a construct it does not know."""
    lexer = _Lexer(pattern)
    body = lexer.parse()
    unwrapped = _unwrap(body)
    if unwrapped.kind in ("literal", "class", "shorthand", "any", "anchor"):
        detail = {**unwrapped.detail, "source": pattern}
        return Node(unwrapped.kind, unwrapped.span, detail, unwrapped.children)
    detail = {
        "flags": "".join(sorted(lexer.global_flags)),
        "groups": lexer.groups,
        "source": pattern,
    }
    return Node("pattern", (0, len(pattern)), detail, (body,))


def walk(node: Node) -> Iterator[Node]:
    """Every node of the tree in pattern order (parent before children)."""
    yield node
    for child in node.children:
        yield from walk(child)


def sre_shape(pattern: str, flags: int) -> dict[str, int]:
    """Group, lookaround and backreference counts from Python's own parser."""
    parser = importlib.import_module("re._parser")
    constants = importlib.import_module("re._constants")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        parsed = parser.parse(pattern, flags)
    shape = {"groups": parsed.state.groups - 1}

    def visit(value: object) -> None:
        if isinstance(value, parser.SubPattern):
            for op, argument in value:
                key = None
                if op is constants.ASSERT:
                    key = "lookahead" if argument[0] == 1 else "lookbehind"
                elif op is constants.ASSERT_NOT:
                    key = "negative_lookahead" if argument[0] == 1 else "negative_lookbehind"
                elif op in (constants.GROUPREF, constants.GROUPREF_EXISTS):
                    key = "backref"
                if key is not None:
                    shape[key] = shape.get(key, 0) + 1
                visit(argument)
        elif isinstance(value, list | tuple):
            for item in value:
                visit(item)

    visit(parsed)
    return shape


def lex_shape(tree: Node) -> dict[str, int]:
    """The same counts as ``sre_shape``, from the lexer tree."""
    shape = {"groups": 0}
    for node in walk(tree):
        key = None
        if node.kind == "group" and node.detail.get("capture"):
            key = "groups"
        elif node.kind == "look":
            key = ("negative_" if node.detail["negative"] else "") + (
                "lookahead" if node.detail["ahead"] else "lookbehind"
            )
        elif node.kind in ("backref", "conditional"):
            key = "backref"
        if key is not None:
            shape[key] = shape.get(key, 0) + 1
    return shape


def min_width(node: Node) -> int:
    """The least number of characters a match of ``node`` consumes (Python's getwidth()[0])."""
    kind = node.kind
    if kind in ("literal", "class", "shorthand", "any"):
        return 1
    if kind in ("anchor", "look"):
        return 0
    if kind == "alt":
        return min(min_width(child) for child in node.children)
    if kind == "repeat":
        return int(str(node.detail["min"])) * min_width(node.children[0])
    if kind == "backref":
        return int(str(node.detail["width"]))
    if kind == "conditional":
        widths = [min_width(child) for child in node.children]
        return min(widths) if node.detail["two"] else 0
    return sum(min_width(child) for child in node.children)


def top_level_alternatives(tree: Node) -> tuple[Node, ...]:
    body = tree.children[0] if tree.kind == "pattern" else tree
    while body.kind == "group" and not body.detail.get("capture") and len(body.children) == 1:
        if body.children[0].kind != "alt":
            break
        body = body.children[0]
    return body.children if body.kind == "alt" else (body,)


def _external_ci(consumer: str) -> bool:
    return "case_insensitive" in CONSUMER_FLAGS.get(consumer, frozenset())


def atom_text(node: Node, consumer: str, source: str) -> str:
    """The standalone text of an engine atom, with ``(?i:...)`` under an effective ``i``."""
    raw = source[node.span[0] : node.span[1]] if node.kind != "class" else _class_raw(node, source)
    ci = bool(node.detail.get("ci")) or _external_ci(consumer)
    return f"(?i:{raw})" if ci else raw


def _class_raw(node: Node, source: str) -> str:
    return source[node.span[0] : node.span[1]]


def _is_engine_atom(node: Node, consumer: str) -> bool:
    ci = bool(node.detail.get("ci")) or _external_ci(consumer)
    if node.kind == "shorthand":
        return True
    if node.kind == "literal":
        return ci
    if node.kind == "class":
        return ci or any(member[0] == "shorthand" for member in class_members(node))
    return False


def atom_construct(node: Node, consumer: str) -> str:
    if node.kind == "shorthand":
        return SHORTHAND_CONSTRUCTS[str(node.detail["text"])[1]]
    if node.kind == "class" and not (node.detail.get("ci") or _external_ci(consumer)):
        for member in class_members(node):
            if member[0] == "shorthand":
                return SHORTHAND_CONSTRUCTS[str(member[1])[1]]
    return "inline-flag-i"


def atom_nodes(tree: Node, consumer: str) -> list[Node]:
    return [node for node in walk(tree) if _is_engine_atom(node, consumer)]


def engine_atoms(tree: Node, consumer: str) -> list[tuple[str, str]]:
    """(construct, atom text) for every engine-dependent atom, in pattern order."""
    source = _source(tree)
    return [
        (atom_construct(node, consumer), atom_text(node, consumer, source))
        for node in atom_nodes(tree, consumer)
    ]


def _source(tree: Node) -> str:
    text = tree.detail.get("source")
    return str(text) if text is not None else ""


def boundary_atoms(tree: Node, consumer: str) -> list[tuple[str, str, Node]]:
    """(construct, atom text, top-level alternative) for every ``\\b`` and ``\\B``."""
    found: list[tuple[str, str, Node]] = []
    for alternative in top_level_alternatives(tree):
        for node in walk(alternative):
            if node.kind == "anchor" and node.detail["kind"] in ("\\b", "\\B"):
                construct = "word-boundary" if node.detail["kind"] == "\\b" else "non-word-boundary"
                ci = bool(node.detail.get("ci")) or _external_ci(consumer)
                text = str(node.detail["kind"])
                found.append((construct, f"(?i:{text})" if ci else text, alternative))
    return found


def single_match_only(tree: Node, consumer: str) -> bool:
    """Every top-level alternative starts at the start of the subject, so it matches once."""
    for alternative in top_level_alternatives(tree):
        items = alternative.children if alternative.kind == "seq" else (alternative,)
        first = items[0] if items else None
        anchored = (
            first is not None
            and first.kind == "anchor"
            and (
                first.detail["kind"] == "\\A"
                or (first.detail["kind"] == "^" and not first.detail["multiline"])
            )
        )
        if not anchored:
            return False
    return True


def flag_inert(tree: Node, consumer: str, flag: str) -> bool:
    """Whether an external flag cannot change any result of the pattern (Python measured)."""
    # A site the pattern already sets the flag on inline cannot change when it is added outside.
    if flag == "multi_line":
        return not any(
            node.kind == "anchor"
            and node.detail["kind"] in ("^", "$")
            and not node.detail["multiline"]
            for node in walk(tree)
        )
    from tests.contracts.regex_audit_check import python_membership

    engine = CONSUMER_ENGINES[consumer]
    source = _source(tree)
    for node in atom_nodes(tree, consumer):
        if node.detail.get("ci"):
            continue
        text = atom_text(node, consumer, source)
        bare = text[4:-1] if text.startswith("(?i:") else text
        if python_membership(f"(?i:{bare})", engine) != python_membership(bare, engine):
            return False
    return True


def class_matches_newline(node: Node) -> bool:
    """Whether a class can match a newline, decided from its members alone."""
    covered = False
    for member in class_members(node):
        if member[0] == "shorthand":
            covered = covered or member[1] in _NEWLINE_SHORTHANDS
        else:
            covered = covered or int(member[1]) <= 10 <= int(member[2])
    return covered != bool(node.detail["negated"])


def basis_members(atom: Node, consumer: str) -> tuple[str, ...] | None:
    """The basis atoms an atom's Python membership depends on, or None outside the basis."""
    low, high = IGNORE_CASE_LITERALS
    ci = bool(atom.detail.get("ci")) or _external_ci(consumer)
    found: list[str] = []

    def fold(point: int) -> bool:
        if not low <= point <= high:
            return False
        found.append(f"(?i:\\x{point:02x})")
        return True

    if atom.kind == "shorthand":
        text = str(atom.detail["text"])
        found.append(f"(?i:{text})" if ci else text)
    elif atom.kind == "literal":
        if ci and not fold(int(str(atom.detail["cp"]))):
            return None
    elif atom.kind == "class":
        for member in class_members(atom):
            if member[0] == "shorthand":
                found.append(f"(?i:{member[1]})" if ci else str(member[1]))
            elif ci:
                for point in range(int(member[1]), int(member[2]) + 1):
                    if not fold(point):
                        return None
    elif atom.kind == "anchor" and atom.detail["kind"] in ("\\b", "\\B"):
        found.append("(?i:\\w)" if ci else "\\w")
    elif atom.kind in ("any", "anchor"):
        return ()
    else:
        return None
    return tuple(dict.fromkeys(found))


def write_report(
    rows: Sequence[tuple[FoundRegex, Verdict]],
    json_path: Path,
    md_path: Path,
    questions_path: Path | None,
    *,
    domain: DomainTable | None = None,
) -> None:
    """Write the JSON report, the Markdown report and the operator questions file.

    ``domain`` supplies the measured sets and offsets the semantic decision rows list. The writer
    lives in regex_audit_record.py, which imports this module.
    """
    from tests.contracts import regex_audit_record

    regex_audit_record.write_report(rows, json_path, md_path, questions_path, domain=domain)
