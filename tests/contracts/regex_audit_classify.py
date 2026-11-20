"""Regex audit: ``classify``, the shape families, the Rust rendering and ``verify``.

Split from regex_audit_check.py (whose entry points call the public names here) to keep both modules
under the line ceiling. Every call of ``derive_probes``, ``work_entry``, ``run_two_engine_driver``
and ``construct_rows`` goes through the regex_audit_check module at call time, so a test that
patches one of them there reaches this module too.
"""

from __future__ import annotations

import dataclasses
import importlib
import itertools
import re
import warnings
from collections.abc import Mapping, Sequence
from typing import Any, cast

import regex

from tests.contracts import regex_audit_check as check
from tests.contracts import regex_audit_tool as tool
from tests.contracts.regex_audit_tool import FoundRegex, Node

# Constructs no translation covers, split by what Rust does with them: a rejected construct does
# not compile in Rust (reason rust-rejected:<id>); a reinterpreted one compiles there with another
# meaning (reason rust-reinterpreted:<id>). The verbose flag is reinterpreted because Rust drops
# whitespace inside a class where Python keeps it, even though its fixture exemplar happens to
# leave Rust an empty class it then rejects.
_RUST_REJECTED = frozenset(
    {
        "backreference",
        "named-backreference",
        "atomic",
        "conditional",
        "octal-escape",
        "named-char",
        "class-backspace",
        "brace-comma-n",
        "literal-brace",
    }
)
_RUST_REINTERPRETED = frozenset({"possessive", "verbose-flag", "posix-class", "unicode-property"})
_SHAPE_SAFE_DOLLAR = ("FULLMATCH", "FULLMATCH-PATH")
_DOLLAR_AS_WRITTEN = ("MATCH-GROUPS-FIRSTLINE", "SEARCH-PKG-M")


def _verdict(kind: str, reason: str | None = None, **values: Any) -> check.Verdict:
    fields: dict[str, Any] = {
        "kind": kind,
        "families": (),
        "python_text": None,
        "rust_text": None,
        "reason": reason,
        "atoms": (),
        "decisions": (),
        "verified": False,
    }
    fields.update(values)
    return check.Verdict(**fields)


# ---------------------------------------------------------------------------------------------
# The Rust-safe syntax filter
# ---------------------------------------------------------------------------------------------


def rust_safe(text: str) -> list[str]:
    """The construct ids in ``text`` that Rust rejects or reads differently from Python."""
    found: list[str] = []
    index, size, in_class, class_start = 0, len(text), False, 0
    while index < size:
        character = text[index]
        pair = text[index : index + 2]
        if character == "\\" and index + 1 < size:
            following = text[index + 1]
            if following in "xu" and text[index + 2 : index + 3] == "{":
                end = text.find("}", index)
                index = end + 1 if end > 0 else size
                continue
            if not in_class:
                if following == "Z":
                    found.append("end-of-string-Z")
                elif following in "<>":
                    found.append("escaped-angle")
                elif following.isdigit():
                    digits = re.match(r"[0-7]{3}", text[index + 1 :])
                    found.append("octal-escape" if following == "0" or digits else "backreference")
                elif following == "N":
                    found.append("named-char")
                elif following in "pP":
                    found.append("unicode-property")
            elif following == "b":
                found.append("class-backspace")
            index += 2
            continue
        if in_class:
            if character == "]" and index > class_start:
                in_class = False
            elif pair == "[:":
                found.append("posix-class")
            elif character == "[":
                found.append("bracket-in-class")
            elif pair in ("&&", "--", "~~"):
                found.append(
                    {"&&": "class-intersection", "--": "class-difference", "~~": "class-symmetric"}[
                        pair
                    ]
                )
                index += 2
                continue
            index += 1
            continue
        if character == "[":
            in_class = True
            index += 1
            if text[index : index + 1] == "^":
                index += 1
            class_start = index + 1 if text[index : index + 1] == "]" else index
            index = class_start
            continue
        group = re.match(r"\(\?(<=|<!|=|!|>|\(|P=|<[A-Za-z_]|[a-zA-Z-]*x)", text[index:])
        if group is not None:
            head = group.group(1)
            found.append(
                {
                    "=": "lookahead",
                    "!": "negative-lookahead",
                    "<=": "lookbehind",
                    "<!": "negative-lookbehind",
                    ">": "atomic",
                    "(": "conditional",
                    "P=": "named-backreference",
                }.get(head, "named-group-angle" if head.startswith("<") else "verbose-flag")
            )
        elif character in "*+?}" and text[index + 1 : index + 2] == "+":
            found.append("possessive")
            index += 2
            continue
        elif character == "{":
            if text[index : index + 2] == "{,":
                found.append("brace-comma-n")
            elif re.match(r"\{\d+(,\d*)?\}", text[index:]) is None:
                found.append("literal-brace")
        index += 1
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------------------------
# Rendering a tree with replacements
# ---------------------------------------------------------------------------------------------

Replacements = dict[tuple[tuple[int, int], str], str]
# A lookahead consumed in place: rendered as a non-capturing group of its rendered body.
_CONSUME = "\x00consume"


def _key(node: Node) -> tuple[tuple[int, int], str]:
    return (node.span, node.kind)


def _render(node: Node, source: str, replace: Replacements) -> str:
    if replace.get(_key(node)) == _CONSUME:
        return "(?:" + _render(node.children[0], source, replace) + ")"
    if _key(node) in replace:
        return replace[_key(node)]
    if not node.children:
        return source[node.span[0] : node.span[1]]
    parts: list[str] = []
    cursor = node.span[0]
    for child in node.children:
        parts.append(source[cursor : child.span[0]])
        parts.append(_render(child, source, replace))
        cursor = child.span[1]
    parts.append(source[cursor : node.span[1]])
    return "".join(parts)


def _render_text(tree: Node, source: str, replace: Replacements) -> str:
    """The whole pattern text: an unwrapped root keeps the text around its span (global flags)."""
    return source[: tree.span[0]] + _render(tree, source, replace) + source[tree.span[1] :]


def _ranges_text(ranges: Sequence[tuple[int, int]]) -> str:
    # Ranges that meet across the surrogate gap are written as one range: Rust's class negation
    # reads a split pair (`...-\x{D7FF}\x{E000}-...`) as leaving U+D7FF and U+E000 outside it.
    joined: list[tuple[int, int]] = []
    for low, high in ranges:
        if joined and joined[-1][1] == 0xD7FF and low == 0xE000:
            joined[-1] = (joined[-1][0], high)
        else:
            joined.append((low, high))
    ranges = joined
    return "".join(
        f"\\x{{{low:X}}}" if low == high else f"\\x{{{low:X}}}-\\x{{{high:X}}}"
        for low, high in ranges
    )


def _class_member_text(raw: str) -> str:
    return raw if raw.startswith("\\") or raw not in "-&~[]^" else "\\" + raw


def _render_members(members: Sequence[tuple[object, ...]], shorthand: Mapping[str, str]) -> str:
    parts = []
    for member in members:
        if member[0] == "shorthand":
            parts.append(shorthand.get(str(member[1]), str(member[2])))
        elif member[1] == member[2] and member[3] == member[4]:
            parts.append(_class_member_text(str(member[3])))
        else:
            parts.append(
                _class_member_text(str(member[3])) + "-" + _class_member_text(str(member[4]))
            )
    return "".join(parts)


def _class_hazard(raw: str) -> bool:
    body = re.sub(r"\\.", "", raw[1:-1])
    body = body[1:] if body.startswith("^") else body
    return "[" in body or any(op in body for op in ("&&", "--", "~~"))


def _domain_sets(
    text: str, consumer: str, domain: check.DomainTable
) -> tuple[check.Ranges, check.Ranges] | None:
    """(Python set, Rust set) of a stable atom over the consumer's subject domain, or None."""
    if not domain.version_stable(consumer, tool.lex(text)):
        return None
    python = domain.python_for(consumer, text)
    rust = domain.rust_for(text)
    if tool.SUBJECT_DOMAINS.get(consumer) == "scalar-first-line":
        python = check._combine(python, ((10, 10),), "sub")
        rust = check._combine(rust, ((10, 10),), "sub")
    return python, rust


def _translation(node: Node, source: str, consumer: str, domain: check.DomainTable) -> str | None:
    """The T-DOM text of a stable atom whose sets differ, or None when it would exceed the limit."""
    text = tool.atom_text(node, consumer, source)
    sets = _domain_sets(text, consumer, domain)
    if sets is None or sets[0] == sets[1]:
        return None
    python, rust = sets
    difference = check._combine(python, rust, "xor")
    ci = bool(node.detail.get("ci")) or "case_insensitive" in tool.CONSUMER_FLAGS[consumer]
    if node.kind == "class" and not ci:
        members = tool.class_members(node)
        inlined = {
            str(m[1]): _ranges_text(domain.python_for(consumer, str(m[1])))
            for m in members
            if m[0] == "shorthand"
        }
        body = _render_members(members, inlined)
        emitted = len(re.findall(r"\\x\{[0-9A-F]+\}(?:-\\x\{[0-9A-F]+\})?", body)) + sum(
            1 for m in members if m[0] != "shorthand"
        )
        rendered = "[" + ("^" if node.detail["negated"] else "") + body + "]"
    else:
        # The emitted set is the whole Python set; the first-line domain only narrows the
        # comparison above, since the Rust text must still read every scalar the way Python does.
        full = domain.python_for(consumer, text)
        emitted = len(full)
        rendered = "[" + _ranges_text(full) + "]"
        if ci:
            rendered = "(?-i:" + rendered + ")"
    limit = tool.DOMAIN_TRANSLATION_MAX_RANGES
    if emitted > limit or len(difference) > limit:
        return None
    return rendered


def _rust_render(
    python_text: str, consumer: str, domain: check.DomainTable
) -> tuple[str, tuple[str, ...]]:
    tree = tool.lex(python_text)
    replace: Replacements = {}
    families: list[str] = []
    for node in tool.walk(tree):
        translated = None
        if tool._is_engine_atom(node, consumer):
            try:
                translated = _translation(node, python_text, consumer, domain)
            except ValueError:
                translated = None
        if translated is not None:
            replace[_key(node)] = translated
            families.append("T-DOM")
        elif node.kind == "anchor" and node.detail["kind"] == "\\Z":
            replace[_key(node)] = "\\z"
            families.append("T-END")
        elif node.kind == "literal" and node.detail["text"] in ("\\<", "\\>"):
            replace[_key(node)] = str(node.detail["text"])[1]
            families.append("T-ESC")
        elif node.kind == "class" and _class_hazard(python_text[node.span[0] : node.span[1]]):
            body = _render_members(tool.class_members(node), {})
            replace[_key(node)] = "[" + ("^" if node.detail["negated"] else "") + body + "]"
            families.append("T-CLASS")
    return _render_text(tree, python_text, replace), tuple(dict.fromkeys(families))


def to_rust_text(python_text: str, consumer: str, domain: check.DomainTable) -> str:
    return _rust_render(python_text, consumer, domain)[0]


# ---------------------------------------------------------------------------------------------
# R5 independence
# ---------------------------------------------------------------------------------------------


def literals_independent(x: str, y: str, flags: int, domain: check.DomainTable) -> bool | None:
    """Whether no occurrence of one literal can overlap one of the other.

    Under ``re.I`` each character's fold set is read only when its basis rows prove it equal on
    every supported minor; otherwise the answer is None and no set is read.
    """
    folding = bool(flags & re.IGNORECASE)
    folds: dict[str, set[int]] = {}
    if folding:
        texts = {character: f"(?i:{re.escape(character)})" for character in x + y}
        for text in texts.values():
            members = tool.basis_members(tool.lex(text), "BOOL")
            if not domain.basis_stable("BOOL", members):
                return None
        for character, text in texts.items():
            key = ("re", text)
            if key not in domain.python:
                return None
            folds[character] = check._points(domain.python[key])

    def same(a: str, b: str) -> bool:
        return bool(folds[a] & folds[b]) if folding else a == b

    def fits(inner: str, outer: str) -> bool:
        return any(
            all(same(inner[i], outer[start + i]) for i in range(len(inner)))
            for start in range(len(outer) - len(inner) + 1)
        )

    for first, second in ((x, y), (y, x)):
        if fits(first, second):
            return False
        for size in range(1, min(len(first), len(second))):
            if all(same(first[-size + i], second[i]) for i in range(size)):
                return False
    return True


# ---------------------------------------------------------------------------------------------
# Shape: lookaround and `$` sites
# ---------------------------------------------------------------------------------------------


def _single_atom(body: Node) -> Node | None:
    node = body.children[0] if body.kind == "seq" and len(body.children) == 1 else body
    return node if node.kind in ("class", "literal") else None


def _vacuous(body: Node) -> bool:
    items = body.children if body.kind == "seq" else (body,)
    return all(item.kind == "repeat" and item.detail["min"] == 0 for item in items)


def _r5_literal(body: Node) -> list[Node] | None:
    items = body.children if body.kind == "seq" else ()
    if len(items) < 2:
        return None
    head = items[0]
    if not (head.kind == "repeat" and head.detail["min"] == 0 and head.detail["max"] is None):
        return None
    if head.children[0].kind != "any" or head.detail["lazy"] or head.detail["possessive"]:
        return None
    rest = list(items[1:])
    return rest if all(item.kind == "literal" for item in rest) else None


def _r1_group(node: Node) -> tuple[Node, Node] | None:
    """(caret branch, lookbehind) of a group `(^|(?<=[C]))`, in either order."""
    if node.kind != "group" or node.children[0].kind != "alt":
        return None
    branches = node.children[0].children
    if len(branches) != 2:
        return None
    unwrap = [b.children[0] if b.kind == "seq" and len(b.children) == 1 else b for b in branches]
    carets = [u for u in unwrap if u.kind == "anchor" and u.detail["kind"] == "^"]
    looks = [
        u
        for u in unwrap
        if u.kind == "look"
        and not u.detail["ahead"]
        and not u.detail["negative"]
        and _single_atom(u.children[0]) is not None
    ]
    return (carets[0], looks[0]) if carets and looks else None


class _Sites:
    """The lookaround and `$` sites of a tree, each with its position facts."""

    def __init__(self, tree: Node) -> None:
        self.sites: list[tuple[Node, dict[str, object]]] = []
        for alternative in tool.top_level_alternatives(tree):
            items = alternative.children if alternative.kind == "seq" else (alternative,)
            for index, item in enumerate(items):
                facts = {"items": items, "index": index}
                self.visit(item, facts, repeat=False, look=False, final=index == len(items) - 1)

    def visit(
        self, node: Node, top: dict[str, object] | None, *, repeat: bool, look: bool, final: bool
    ) -> None:
        if node.kind == "look" or (node.kind == "anchor" and node.detail["kind"] == "$"):
            facts: dict[str, object] = {"repeat": repeat, "look": look, "final": final, "top": top}
            self.sites.append((node, facts))
        inner_look = look or node.kind == "look"
        if node.kind == "repeat":
            self.visit(node.children[0], None, repeat=True, look=inner_look, final=False)
        elif node.kind == "seq":
            for index, child in enumerate(node.children):
                last = final and index == len(node.children) - 1
                self.visit(child, None, repeat=repeat, look=inner_look, final=last)
        else:
            for child in node.children:
                self.visit(child, None, repeat=repeat, look=inner_look, final=final)


def _r5(
    run: Sequence[Node], source: str, consumer: str, domain: check.DomainTable
) -> tuple[str | None, str | None]:
    """(reason, replacement) for a run of two or more final positive lookaheads."""
    literals = [_r5_literal(look.children[0]) for look in run]
    if any(literal is None for literal in literals):
        return "lookaround-unsupported-shape", None
    nodes = [literal for literal in literals if literal is not None]
    if any(int(str(n.detail["cp"])) == 10 for literal in nodes for n in literal):
        return "lookaround-newline-body", None
    external = "case_insensitive" in tool.CONSUMER_FLAGS[consumer]
    ci = external or any(bool(n.detail["ci"]) for literal in nodes for n in literal)
    if ci:
        for literal in nodes:
            for n in literal:
                text = "(?i:" + re.escape(chr(int(str(n.detail["cp"])))) + ")"
                if not domain.basis_stable(consumer, tool.basis_members(tool.lex(text), consumer)):
                    return "lookaround-fold-not-version-stable", None
    strings = ["".join(chr(int(str(n.detail["cp"]))) for n in literal) for literal in nodes]
    flags = (re.IGNORECASE if ci else 0) | check.consumer_flags("BOOL")
    for first, second in itertools.combinations(strings, 2):
        independent = literals_independent(first, second, flags, domain)
        if independent is None:
            return "lookaround-fold-not-version-stable", None
        if not independent:
            return "lookaround-overlapping-literals", None
    bodies = [_render(look.children[0], source, {}) for look in run]
    arms = ["".join(order) for order in itertools.permutations(bodies)]
    return None, "(?:" + "|".join(arms) + ")"


def _negative_final(look: Node, source: str) -> tuple[str | None, str | None, str]:
    body = look.children[0]
    atom = _single_atom(body)
    if atom is not None and atom.kind == "class":
        if tool.class_matches_newline(atom):
            return "lookaround-newline-body", None, "R2-"
        if atom.detail["negated"]:
            return "lookaround-unsupported-shape", None, "R2-"
        inner = source[atom.span[0] + 1 : atom.span[1] - 1]
        return None, f"(?:[^{inner}]|\\Z)", "R2-"
    items = body.children if body.kind == "seq" else (body,)
    if items and all(item.kind == "literal" for item in items):
        if any(int(str(item.detail["cp"])) == 10 for item in items):
            return "lookaround-newline-body", None, "R4"
        raw = [str(item.detail["text"]) for item in items]
        if len(raw) == 1:
            return None, f"(?:[^{raw[0]}]|\\Z)", "R2-"
        arms = ["".join(raw[:i]) + f"[^{raw[i]}]" for i in range(len(raw))]
        arms += ["".join(raw[:i]) + "\\Z" for i in range(len(raw))]
        return None, "(?:" + "|".join(arms) + ")", "R4"
    return "lookaround-unsupported-shape", None, "R4"


def _shape(
    tree: Node, source: str, consumer: str, domain: check.DomainTable
) -> tuple[str | None, Replacements, list[str]]:
    """(first shape reason, replacements, families) of the lookaround and `$` sites."""
    replace: Replacements = {}
    families: list[str] = []
    handled: set[tuple[tuple[int, int], str]] = set()
    sites = sorted(_Sites(tree).sites, key=lambda site: site[0].span[0])
    groups: dict[tuple[tuple[int, int], str], tuple[Node, Node, Node]] = {}
    for alternative in tool.top_level_alternatives(tree):
        items = alternative.children if alternative.kind == "seq" else (alternative,)
        pair = _r1_group(items[0]) if items else None
        if pair is not None:
            groups[_key(pair[1])] = (items[0], *pair)
    for node, facts in sites:
        if _key(node) in handled:
            continue
        top = facts["top"]
        if node.kind == "anchor":
            if node.detail["multiline"] or consumer in _DOLLAR_AS_WRITTEN:
                continue
            if consumer in _SHAPE_SAFE_DOLLAR and facts["final"] and not facts["repeat"]:
                continue
            if consumer == "BOOL" and facts["final"] and not facts["repeat"]:
                replace[_key(node)] = "(?:\\n?\\Z)"
                families.append("D1")
                continue
            return "dollar-before-final-newline", replace, families
        if consumer != "BOOL":
            return "lookaround-non-boolean", replace, families
        if facts["repeat"]:
            return "lookaround-under-repetition", replace, families
        if facts["look"]:
            return "lookaround-unsupported-shape", replace, families
        body = node.children[0]
        if any(n.kind == "group" and n.detail.get("capture") for n in tool.walk(body)):
            return "lookaround-with-groups", replace, families
        ahead, negative = bool(node.detail["ahead"]), bool(node.detail["negative"])
        if _key(node) in groups:
            group, caret, look = groups[_key(node)]
            arms = [
                _render(caret, source, {})
                if part is caret
                else _render(look.children[0], source, {})
                for part in sorted((caret, look), key=lambda n: n.span[0])
            ]
            replace[_key(group)] = "(?:" + "|".join(arms) + ")"
            families.append("R1")
            continue
        if not ahead:
            atom = _single_atom(body)
            if not (isinstance(top, dict) and top["index"] == 0 and atom is not None):
                return "lookaround-unsupported-shape", replace, families
            raw = source[atom.span[0] : atom.span[1]]
            if not negative:
                replace[_key(node)] = raw
                families.append("R1")
                continue
            if atom.kind == "class":
                inner = raw[1:-1]
                negated_text = "[" + inner[1:] + "]" if atom.detail["negated"] else f"[^{inner}]"
            else:
                negated_text = f"[^{raw}]"
            replace[_key(node)] = f"(?:\\A|{negated_text})"
            families.append("R3")
            continue
        if _vacuous(body):
            if negative or not isinstance(top, dict):
                return "lookaround-unsupported-shape", replace, families
            replace[_key(node)] = ""
            families.append("R0")
            continue
        if not isinstance(top, dict):
            return "lookaround-unsupported-shape", replace, families
        run_items = cast(Sequence[Node], top["items"])
        index = int(str(top["index"]))
        start = index
        while (
            start > 0
            and run_items[start - 1].kind == "look"
            and run_items[start - 1].detail["ahead"]
        ):
            start -= 1
        tail = list(run_items[start:])
        if not all(item.kind == "look" and item.detail["ahead"] for item in tail):
            return "lookaround-continuation", replace, families
        run = [
            item
            for item in tail
            if not (_vacuous(item.children[0]) and not item.detail["negative"])
        ]
        if len(run) >= 2:
            if any(item.detail["negative"] for item in run):
                return "lookaround-unsupported-shape", replace, families
            reason, text = _r5(run, source, consumer, domain)
            if reason is not None:
                return reason, replace, families
            replace[_key(run[0])] = str(text)
            for other in run[1:]:
                replace[_key(other)] = ""
                handled.add(_key(other))
            families.append("R5")
            continue
        if not negative:
            replace[_key(node)] = _CONSUME
            families.append("R2+")
            continue
        reason, text, family = _negative_final(node, source)
        if reason is not None:
            return reason, replace, families
        replace[_key(node)] = str(text)
        families.append(family)
    return None, replace, families


# ---------------------------------------------------------------------------------------------
# Atoms: version stability, rules 1 to 4 and the boundary conditions
# ---------------------------------------------------------------------------------------------


class _Atoms:
    """The atom pass of one pattern: reason ids in pattern order, decisions and boundary rows."""

    def __init__(self, tree: Node, source: str, consumer: str, domain: check.DomainTable) -> None:
        self.consumer, self.domain = consumer, domain
        self.engine = tool.CONSUMER_ENGINES[consumer]
        self.accepted = tool.ACCEPTED_DIVERGENCES
        self.version_reason: str | None = None
        self.general_reason: str | None = None
        self.decisions: list[str] = []
        self.atoms: list[str] = []
        self.boundaries: list[dict[str, str]] = []
        alternatives = {
            id(node): alternative
            for alternative in tool.top_level_alternatives(tree)
            for node in tool.walk(alternative)
        }
        for node in tool.walk(tree):
            if tool._is_engine_atom(node, consumer):
                text = tool.atom_text(node, consumer, source)
                self.atom(tool.atom_construct(node, consumer), text, node, source)
            elif node.kind == "anchor" and node.detail["kind"] in ("\\b", "\\B"):
                self.boundary(node, alternatives.get(id(node), tree))

    def decide(self, identifier: str, *, version: bool) -> None:
        self.decisions.append(identifier)
        if version and self.version_reason is None:
            self.version_reason = identifier
        if not version and self.general_reason is None:
            self.general_reason = identifier

    def unstable(self, general: str) -> bool:
        """Record an unstable atom; True when acceptance makes it exact as written."""
        version = general + "-version-difference"
        if version in self.accepted and general in self.accepted:
            self.atoms += [version, general]
            return True
        self.decide(general if version in self.accepted else version, version=True)
        return False

    def atom(self, construct: str, text: str, node: Node, source: str) -> None:
        general = f"{self.engine}:{construct}"
        self.atoms.append(general)
        if not self.domain.version_stable(self.consumer, tool.lex(text)):
            self.unstable(general)
            return
        sets = _domain_sets(text, self.consumer, self.domain)
        if sets is None or sets[0] == sets[1]:
            return
        if general in self.accepted:
            return
        if _translation(node, source, self.consumer, self.domain) is None:
            self.decide(general, version=False)

    def boundary(self, node: Node, alternative: Node) -> None:
        construct = "word-boundary" if node.detail["kind"] == "\\b" else "non-word-boundary"
        general = f"{self.engine}:{construct}"
        self.atoms.append(general)
        ci = bool(node.detail.get("ci")) or "case_insensitive" in tool.CONSUMER_FLAGS[self.consumer]
        text = f"(?i:{node.detail['kind']})" if ci else str(node.detail["kind"])
        word = "(?i:\\w)" if ci else "\\w"
        if not self.domain.version_stable(self.consumer, tool.lex(text)):
            self.unstable(general)
            self.boundaries.append({"membership": "version-difference"})
            return
        row: dict[str, str] = {}
        equal = self.domain.python_for(self.consumer, word) == self.domain.rust_for(word)
        row["membership"] = (
            "equal" if equal else "accepted" if general in self.accepted else "decision"
        )
        for group in ("nonword_only", "word_edge"):
            row[group] = (
                "equal" if self.domain.boundary_holds(self.consumer, text, group) else "decision"
            )
        empty_id = general + "-empty-subject"
        if self.domain.boundary_holds(self.consumer, text, "empty"):
            row["empty"] = "equal"
        elif tool.min_width(alternative) >= 1:
            row["empty"] = "restricted:nonempty-alternative"
        elif empty_id in self.accepted:
            row["empty"] = "accepted"
            self.atoms.append(empty_id)
        else:
            row["empty"] = "decision"
        self.boundaries.append(row)
        if "decision" in (row["membership"], row["nonword_only"], row["word_edge"]):
            if not (general in self.accepted and row["membership"] != "decision"):
                self.decide(general, version=False)
        if row["empty"] == "decision":
            self.decide(empty_id, version=False)

    def reason(self) -> str | None:
        chosen = self.version_reason or self.general_reason
        return None if chosen is None else check.SEMANTIC_PREFIX + chosen


# ---------------------------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------------------------


def _python_valid(pattern: str, consumer: str) -> bool:
    try:
        check._compiled(consumer, pattern)
    except (re.error, regex.error):
        return False
    if tool.CONSUMER_ENGINES[consumer] == "regex":
        return True
    return not check.python_invalid_minors(pattern, check.construct_rows())


def _unsupported(tree: Node, source: str) -> str | None:
    for node in tool.walk(tree):
        raw = source[node.span[0] : node.span[1]]
        if node.kind == "backref":
            return "named-backreference" if node.detail["named"] else "backreference"
        if node.kind in ("atomic", "conditional"):
            return str(node.kind)
        if node.kind == "repeat" and node.detail["possessive"]:
            return "possessive"
        if node.kind == "repeat" and re.search(r"\{,", raw[-8:]):
            return "brace-comma-n"
        if node.kind == "literal":
            text = str(node.detail["text"])
            if re.fullmatch(r"\\[0-7]{1,3}", text):
                return "octal-escape"
            if text.startswith("\\N"):
                return "named-char"
            if text[:2] in ("\\p", "\\P"):
                return "unicode-property"
            if text == "{":
                return "literal-brace"
        if node.kind == "class":
            if re.search(r"\[:\^?[A-Za-z]+:\]", raw[1:]):
                return "posix-class"
            members = tool.class_members(node)
            if any(m[0] == "range" and "\\b" in (m[3], m[4]) for m in members):
                return "class-backspace"
    flags = str(tree.detail.get("flags", "")) if tree.kind == "pattern" else ""
    if "x" in flags or re.search(r"\(\?[a-zA-Z]*x[a-zA-Z-]*[:)]", source):
        return "verbose-flag"
    return None


def _rust_reason(construct: str) -> str:
    """The fail reason for a construct no translation covers: rejected or reinterpreted."""
    if construct in _RUST_REINTERPRETED:
        return f"rust-reinterpreted:{construct}"
    if construct in _RUST_REJECTED:
        return f"rust-rejected:{construct}"
    raise ValueError(f"construct {construct!r} has no Rust disposition")


def _parser_agrees(tree: Node, pattern: str, consumer: str) -> bool:
    """Whether the lexer tree and Python's own parser agree on the pattern's structure.

    The comparison is the lexer test's: group, lookaround and backreference counts and the minimum
    width, under the consumer's flags (none for the regex package's class, whose flags ``re`` does
    not read). A pattern ``re._parser`` cannot read, which only the regex package accepts, has
    nothing to compare.
    """
    flags = 0 if consumer == "SEARCH-PKG-M" else check.consumer_flags(consumer)
    parser = importlib.import_module("re._parser")
    try:
        shape = tool.sre_shape(pattern, flags)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            width = int(parser.parse(pattern, flags).getwidth()[0])
    except re.error:
        return True
    return tool.lex_shape(tree) == shape and tool.min_width(tree) == width


def classify(pattern: str, consumer: str, domain: check.DomainTable) -> check.Verdict:
    """The verdict for one pattern under its consumer class, in the order of reasons."""
    try:
        tree = tool.lex(pattern)
    except tool.UnknownConstruct as unknown:
        return _verdict("fail", f"unknown-construct:{unknown}")
    if not _python_valid(pattern, consumer):
        return _verdict("fail", "python-invalid")
    if not _parser_agrees(tree, pattern, consumer):
        return _verdict("fail", "parser-disagreement")
    unsupported = _unsupported(tree, pattern)
    if unsupported is not None:
        return _verdict("fail", _rust_reason(unsupported))
    atoms = _Atoms(tree, pattern, consumer, domain)
    common = {
        "atoms": tuple(dict.fromkeys(atoms.atoms)),
        "decisions": tuple(dict.fromkeys(atoms.decisions)),
        "boundaries": tuple(atoms.boundaries),
    }
    if consumer == "INERT-EXEMPTION":
        return _verdict("reported", "inert-no-command-pattern", **common)
    shape_reason, replace, families = _shape(tree, pattern, consumer, domain)
    if shape_reason is None and consumer == "SEARCH-PKG-M":
        try:
            check._compiled("BOOL", pattern)
        except re.error:
            shape_reason = "search-package-syntax"
    if shape_reason is not None:
        return _verdict("reported", shape_reason, **common)
    if consumer == "SUB" and tool.min_width(tree) == 0:
        return _verdict("reported", "sub-empty-match-iteration", **common)
    atom_reason = atoms.reason()
    if atom_reason is not None:
        return _verdict("reported", atom_reason, **common)
    python_text = _render_text(tree, pattern, replace) if replace else pattern
    universal = "R0" in families and check.matches_everything(python_text, consumer)
    probes = check.derive_probes(pattern, python_text, consumer)
    if families:
        # A claimed rewrite that disagrees with the original on a probe is a wrong proof, not a
        # shape to report: the gate fails with the first input on which the two differ.
        differing = check.check_rewrite(pattern, python_text, consumer, probes)
        if differing is not None:
            return _verdict("fail", f"rewrite-mismatch:{differing!r}")
    witness = _witness_reason(tree, pattern, consumer, families, universal, probes)
    if witness is not None:
        proposal = python_text if families else None
        return _verdict("reported", witness, python_text=proposal, **common)
    rust_text, rust_families = _rust_render(python_text, consumer, domain)
    all_families = tuple(dict.fromkeys([*families, *rust_families]))
    return _verdict(
        "rewrite" if all_families else "as_written",
        None,
        families=all_families,
        python_text=python_text,
        rust_text=rust_text,
        universal=universal,
        **common,
    )


def _sub_matches(value: object) -> Sequence[object]:
    """The matches of a SUB observable, which is (matches, replaced)."""
    return cast(tuple[Sequence[object], str], value)[0]


def _witness_reason(
    tree: Node,
    pattern: str,
    consumer: str,
    families: Sequence[str],
    universal: bool,
    probes: Sequence[str],
) -> str | None:
    if families:
        truths = [check.observable(consumer, pattern, probe) for probe in probes]
        if universal:
            if not all(truths):
                return "no-distinguishing-witness"
        elif not (True in truths and False in truths):
            return "no-distinguishing-witness"
    if consumer == "SUB" and not tool.single_match_only(tree, consumer):
        counts = [len(_sub_matches(check.observable(consumer, pattern, p))) for p in probes]
        if not any(count >= 2 for count in counts):
            return "no-multi-match-witness"
    for flag in tool.CONSUMER_FLAGS.get(consumer, frozenset()):
        witnessed = any(
            check.observable(consumer, pattern, probe)
            != check.observable(consumer, pattern, probe, without=flag)
            for probe in probes
        )
        if not witnessed and not tool.flag_inert(tree, consumer, flag):
            return "no-flag-witness"
    return None


# ---------------------------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------------------------


def verify(
    rows: Sequence[tuple[FoundRegex, check.Verdict]],
) -> list[tuple[FoundRegex, check.Verdict]]:
    """Send every exact text to the Rust driver and compare each probe with Python."""
    entries: list[Mapping[str, object]] = []
    sent: dict[int, list[str]] = {}
    for index, (item, verdict) in enumerate(rows):
        if verdict.kind not in ("as_written", "rewrite"):
            continue
        probes = check.derive_probes(
            item.pattern, verdict.python_text or item.pattern, item.consumer
        )
        sent[index] = list(probes)
        entries.append(
            check.work_entry(index, verdict.rust_text or item.pattern, item.consumer, probes)
        )
    results = check.run_two_engine_driver(entries, []) if entries else check.DriverResults()
    checked: list[tuple[FoundRegex, check.Verdict]] = []
    for index, (item, verdict) in enumerate(rows):
        if index not in sent:
            checked.append((item, verdict))
            continue
        result = results.patterns.get(index)
        if result is None:
            changed = dataclasses.replace(
                verdict, kind="reported", reason="unverified-proposal", verified=False
            )
        else:
            mismatch = _mismatch(item, sent[index], result)
            if mismatch is None:
                changed = dataclasses.replace(verdict, verified=True)
            else:
                changed = dataclasses.replace(
                    verdict, kind="fail", reason=f"two-engine-mismatch:{mismatch}", verified=False
                )
        checked.append((item, changed))
    return checked


def _mismatch(item: FoundRegex, probes: Sequence[str], result: object) -> str | None:
    if isinstance(result, Mapping):
        return f"driver error {result.get('error')!r}"
    if not isinstance(result, Sequence) or len(result) != len(probes):
        return "probe results missing"
    for probe, value in zip(probes, result, strict=True):
        python = check.observable(item.consumer, item.pattern, probe)
        if check.compare_observable(item.consumer, python, value) is not None:
            return repr(probe)
    return None
