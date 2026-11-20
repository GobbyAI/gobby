"""Regex audit: the consumer drivers, the report writer and the record functions.

``drive_consumer`` runs one case of tests/contracts/regex_audit/consumers.json through the
production consumer its ``driver`` names and returns that consumer's observable. Every driver
takes its pattern from the case (or from ``pattern``, which replaces it), so a test can drive the
same consumer with a rewritten or a deliberately wrong text.

``write_report`` writes the JSON and Markdown reports and the operator questions file from
classified rows. The reports are outputs for the operator, never an asserted fixture.
"""

from __future__ import annotations

import json
import os
import shlex
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

from gobby.agents.detection.safe_regex import RegexOutcome, compile_safe_regex
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_shapes import (
    CLOSURE_EVENTS,
    attestation_shape_refusal,
    select_closure_rules,
)
from gobby.workflows import condition_helpers_paths
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.assistant_response_matching import (
    ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS,
    first_response_match,
)
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine import seat_transitions
from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.templates import TemplateEngine
from tests.contracts import regex_audit_tool as tool
from tests.contracts.regex_audit_tool import FoundRegex

if TYPE_CHECKING:
    from tests.contracts.regex_audit_check import DomainTable, Verdict

RECORD_ENV = "GOBBY_CORPUS_RECORD"
SIDES = ("input", "control")
SYNTHETIC_SESSION = "synthetic-session-seat"
CONSTRUCTS_FIXTURE = Path(__file__).resolve().parent / "regex_audit" / "constructs.json"
NOT_MEASURED = "not measured on every supported minor"

# ---------------------------------------------------------------------------------------------
# Consumer drivers
# ---------------------------------------------------------------------------------------------


def _pattern(case: Mapping[str, Any], pattern: str | None) -> Any:
    return pattern if pattern is not None else case.get("pattern")


def _nested_command(side: str) -> str:
    """``echo x`` wrapped in ``bash -c``: one level past the wrapper bound for the input."""
    depth = SHELL_WRAPPER_DEPTH + 1 if side == "input" else SHELL_WRAPPER_DEPTH
    command = "echo x"
    for _ in range(depth):
        command = f"bash -c {shlex.quote(command)}"
    return command


def _drive_command_patterns(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    command = _nested_command(text) if case.get("nest") else text
    return command_patterns_match(
        command, pattern=_pattern(case, pattern), not_pattern=case.get("not_pattern")
    )


def _drive_rule_engine(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    from tests.contracts import rules_corpus

    effect = dict(case["effect"])
    if pattern is not None:
        effect["command_pattern"] = pattern
    built = {
        "id": f"synthetic-rx-{case['id']}",
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": [],
        "family": None,
        "observer": None,
        "transition": None,
        "seam_failures": [],
        "rules": [
            {"name": "synthetic-rx-bool-effect", "event": "before_tool", "effects": [effect]}
        ],
        "event": {
            "type": "before_tool",
            "source": "claude",
            "data": {"tool_name": "Bash", "tool_input": {"command": text}},
            "metadata": {},
        },
    }
    return rules_corpus.replay_case(built)["outcome_class"]


def _rule_row(index: int, entry: Mapping[str, Any]) -> RuleDefinitionRow:
    from tests.contracts.rules_recorder import FIXED_TIMESTAMP

    body = {key: value for key, value in entry.items() if key != "name"}
    return RuleDefinitionRow(
        id=f"synthetic-row-{index}",
        name=str(entry["name"]),
        enabled=True,
        enabled_pinned=False,
        priority=0,
        definition_json=RuleDefinitionBody.model_validate(body).model_dump(mode="json"),
        source="custom",
        created_at=FIXED_TIMESTAMP,
        updated_at=FIXED_TIMESTAMP,
        deleted_at=None,
    )


def _drive_attestation(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    definitions = case["definitions"]
    entries = [dict(entry) for entry in definitions["rules"]]
    if pattern is not None:
        shape = dict(entries[0]["attestation_shape"])
        shape["reference"] = dict(shape["reference"], value_pattern=pattern)
        entries[0]["attestation_shape"] = shape
    agent = AgentDefinitionBody.model_validate(definitions["agent"])
    rows = [_rule_row(index, entry) for index, entry in enumerate(entries)]
    selected = select_closure_rules(agent, rows, CLOSURE_EVENTS)
    assert [row.id for row in selected] == [rows[0].id], f"{case['id']}: selection {selected}"
    return attestation_shape_refusal(
        kind="synthetic-kind",
        result="pass",
        scheme="synthetic",
        value=text,
        task={},
        agent=agent,
        declared=None,
        rules=rows,
    )


class _SyntheticSessions:
    """The session manager seam: every session id resolves to one synthetic session."""

    def __init__(self, db: object) -> None:
        self._db = db

    def get(self, session_id: str) -> object:
        return SimpleNamespace(id=session_id)


def _drive_advance_seat(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    from tests.contracts.rules_recorder import FIXED_TIMESTAMP

    body = AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-agent-seat",
            "prompts": {"agent": "Synthetic."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": ["track_plan_consensus"],
        }
    )
    message = SimpleNamespace(
        id="synthetic-message-1",
        from_session=SYNTHETIC_SESSION,
        to_session="synthetic-session-peer",
        content=text,
        sent_at=FIXED_TIMESTAMP,
    )
    instance = SimpleNamespace(current_step="synthetic-review", variables={})
    protocol = dict(case["protocol"]) | {"consensus_pattern": _pattern(case, pattern)}
    with (
        mock.patch.object(seat_transitions, "SessionManager", _SyntheticSessions),
        mock.patch.object(
            seat_transitions, "session_agent_definition", lambda db, session: (body, "synthetic")
        ),
        mock.patch.object(seat_transitions, "completion_messages", lambda *args: [message]),
    ):
        return seat_transitions.advance_seat(
            cast(Any, None),
            SYNTHETIC_SESSION,
            cast(Any, instance),
            ("gobby-agents", "synthetic-tool"),
            {},
            {},
            None,
            protocol,
        )


def _drive_first_response(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    if pattern is not None:
        patterns: list[str] = [pattern]
    elif "patterns" in case:
        patterns = list(case["patterns"])
    else:
        patterns = [
            ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS[int(case["patterns_from_constant_index"])]
        ]
    return first_response_match(text, patterns, regex=bool(case["regex"]))


def _drive_safe_regex(case: Mapping[str, Any], text: str, pattern: str | None) -> object:
    result = compile_safe_regex(str(_pattern(case, pattern))).search(text)
    assert result.outcome is not RegexOutcome.PATTERN_TIMEOUT, (
        f"{case['id']}: the search ran past its CPU budget; rerun on an idle machine"
    )
    return result.outcome.value


def _drive_write_paths(
    case: Mapping[str, Any], text: str, pattern: str | None, tmp: Path | None
) -> object:
    assert tmp is not None, f"{case['id']}: the path driver needs a tmp directory"
    root = tmp.resolve() / "r+o.t"
    root.mkdir(parents=True, exist_ok=True)
    event = {"tool_name": "Write", "project_path": str(root), "cwd": str(root)}
    if case.get("home"):
        home = tmp.resolve() / "h+o.me"
        home.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            return condition_helpers_paths.write_paths_match(
                event, {"file_path": str(home / text)}, str(_pattern(case, pattern))
            )
    return condition_helpers_paths.write_paths_match(
        event, {"file_path": str(root / text)}, str(_pattern(case, pattern))
    )


def _drive_template(
    case: Mapping[str, Any], text: str, pattern: str | None, template: str
) -> object:
    context = {"t": text, "p": _pattern(case, pattern), "r": case.get("replacement")}
    return TemplateEngine().render(template, context)


_Driver = Callable[[Mapping[str, Any], str, "str | None"], object]
DRIVERS: Mapping[str, _Driver] = {
    "command_patterns_match": _drive_command_patterns,
    "rule_engine": _drive_rule_engine,
    "attestation_shape_refusal": _drive_attestation,
    "advance_seat": _drive_advance_seat,
    "first_response_match": _drive_first_response,
    "compile_safe_regex": _drive_safe_regex,
    "regex_search_filter": lambda case, text, pattern: _drive_template(
        case, text, pattern, "{{ t | regex_search(p) }}"
    ),
    "regex_replace_filter": lambda case, text, pattern: _drive_template(
        case, text, pattern, "{{ t | regex_replace(p, r) }}"
    ),
}


def drive_consumer(
    case: Mapping[str, object],
    text: str,
    *,
    pattern: str | None = None,
    tmp: Path | None = None,
) -> object:
    """The observable of the case's production consumer on ``text``."""
    values = cast(Mapping[str, Any], case)
    driver = str(values["driver"])
    if driver == "write_paths_match":
        return _drive_write_paths(values, text, pattern, tmp)
    if driver not in DRIVERS:
        raise ValueError(f"{values.get('id')}: unknown consumer driver {driver!r}")
    return DRIVERS[driver](values, text, pattern)


def control_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """The case as its control runs: the control's own pattern, effect or regex switch."""
    control = dict(case)
    for key, replaced in (
        ("control_pattern", "pattern"),
        ("control_effect", "effect"),
        ("regex_control", "regex"),
    ):
        if key in case:
            control[replaced] = case[key]
    return control


def _side_value(case: Mapping[str, Any], side: str, tmp: Path) -> object:
    run = control_case(case) if side == "control" else case
    text = str(case[side]) if side in case else side
    return drive_consumer(run, text, tmp=tmp / side)


def record_consumer_cases(path: Path) -> None:
    """Fill every case's ``expect`` block from production; record mode only."""
    if os.environ.get(RECORD_ENV) != "1":
        return
    document = json.loads(path.read_text())
    with TemporaryDirectory() as scratch:
        for index, case in enumerate(document["cases"]):
            base = Path(scratch) / str(index)
            case["expect"] = {side: _side_value(case, side, base) for side in SIDES}
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------------------------
# The live table and the record entry point
# ---------------------------------------------------------------------------------------------


def _lexed(pattern: str) -> tool.Node | None:
    try:
        return tool.lex(pattern)
    except tool.UnknownConstruct:
        return None


def live_domain(found: Sequence[FoundRegex]) -> DomainTable:
    """The table measured for the universe and the engine-dependent exemplars (one driver run)."""
    from tests.contracts import regex_audit_check as check

    constructs = json.loads(CONSTRUCTS_FIXTURE.read_text())
    atoms: set[tuple[str, str]] = set()
    boundaries: set[tuple[str, str]] = set()
    sites = [(item.pattern, item.consumer) for item in found]
    for row in constructs["constructs"]:
        if row["disposition"] == "engine-dependent":
            sites += [(row["exemplar"], consumer) for consumer in ("BOOL", "SEARCH-PKG-M")]
    for pattern, consumer in sites:
        tree = _lexed(pattern)
        if tree is None:
            continue
        engine = tool.CONSUMER_ENGINES[consumer]
        atoms |= {(engine, atom) for _, atom in tool.engine_atoms(tree, consumer)}
        boundaries |= {(engine, atom) for _, atom, _ in tool.boundary_atoms(tree, consumer)}
    return check.domain_table(
        sorted(atoms), sorted(boundaries), constructs["boundary"], constructs["membership"]
    )


def record_regex_audit(out_dir: Path, questions_path: Path, consumers_path: Path) -> None:
    """Classify and verify the universe, write the three outputs, then record the consumer cases."""
    from tests.contracts import regex_audit_check as check

    found = tool.scan_universe()
    domain = live_domain(found)
    rows = check.verify(
        [(item, check.classify(item.pattern, item.consumer, domain)) for item in found]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_report(
        rows,
        out_dir / "regex-audit.json",
        out_dir / "regex-audit.md",
        questions_path,
        domain=domain,
    )
    record_consumer_cases(consumers_path)


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------

DECISION_OPTIONS: Mapping[str, tuple[str, ...]] = {
    "empty-subject": (
        "accept the Rust value (it is also CPython 3.14's and the regex package's)",
        "keep 3.13's value with an empty-subject guard in the port's consumer",
        "rewrite the pattern so the alternative with the boundary consumes a character",
    ),
    "version-difference": (
        "write an explicit class (for \\d, [0-9])",
        "accept the Rust set",
        "add the atoms to the membership basis",
    ),
    "": (
        "keep the Python set through an explicit class",
        "accept the Rust set",
        "restrict the subject",
    ),
}
REASON_OPTIONS: Mapping[str, str] = {
    "lookaround-fold-not-version-stable": (
        "take the site out of the i scope; add the code points to the membership basis when "
        "they measure equal on every supported minor; leave the lookaround to the operator"
    ),
    "sub-empty-match-iteration": (
        "the port's SUB consumer iterates as Python does; rewrite the pattern so it cannot "
        "match the empty string"
    ),
    "no-multi-match-witness": "add a two-match witness or a single-match proof",
    "no-flag-witness": "add a probe the external flag decides, or prove the flag inert",
    "inert-no-command-pattern": "add a command_pattern beside the exemption, or remove it",
    "inert-not-allowlisted": "add the pattern to the allowlist constant, or remove it",
}
DEFAULT_OPTIONS = (
    "rewrite the pattern without the construct; accept the divergence in ACCEPTED_DIVERGENCES; "
    "leave it to the operator"
)
FIXED_ITEMS = (
    "The allowlist exception: the assistant-response regex consumer runs only the patterns of "
    "the production allowlist constant, under re.IGNORECASE; a definition pattern outside it "
    "is inert.",
    "User-authored lookaround: a lookaround in a user-authored rule has no linear-time rewrite "
    "unless a family covers it; the port must refuse it at load time or keep a backtracking "
    "engine for it.",
    "Replacement template: the port's regex_replace expands Python replacement templates "
    "itself (\\1, \\g<name>); a Rust-style expansion leaves them literal.",
    "Membership basis: an atom outside the closed membership basis is not measured on every "
    "supported minor and is reported until the basis grows.",
)


def _decision_kind(decision: str) -> str:
    for suffix in ("empty-subject", "version-difference"):
        if decision.endswith(f"-{suffix}"):
            return suffix
    return ""


def _construct(decision: str) -> tuple[str, str]:
    engine, _, construct = decision.partition(":")
    kind = _decision_kind(decision)
    return engine, construct.removesuffix(f"-{kind}") if kind else construct


def _causes(kind: str, consumer: str, atom: str, domain: DomainTable | None) -> bool:
    """Whether the atom is one the decision is about, not merely of the same construct."""
    if domain is None or kind == "empty-subject":
        return True
    engine = tool.CONSUMER_ENGINES[consumer]
    if kind == "version-difference":
        return not domain.version_stable(consumer, tool.lex(atom))
    return domain.python.get((engine, atom)) != domain.rust.get(atom)


def _decision_atoms(
    decision: str, rows: Sequence[tuple[FoundRegex, Verdict]], domain: DomainTable | None
) -> list[str]:
    """The atoms of the decision's construct that differ (or are not stable) in its patterns."""
    _, construct = _construct(decision)
    kind = _decision_kind(decision)
    found: set[str] = set()
    for item, verdict in rows:
        if decision not in verdict.decisions:
            continue
        tree = _lexed(item.pattern)
        if tree is None:
            continue
        pairs = [(c, a) for c, a, _ in tool.boundary_atoms(tree, item.consumer)]
        pairs += tool.engine_atoms(tree, item.consumer)
        found |= {
            atom
            for name, atom in pairs
            if name == construct and _causes(kind, item.consumer, atom, domain)
        }
    return sorted(found)


def _points(ranges: Sequence[Sequence[int]]) -> int:
    return sum(high - low + 1 for low, high in ranges)


def _fixture_counts() -> dict[tuple[str, str, str], tuple[int, str]]:
    from tests.contracts import regex_audit_check as check

    membership = json.loads(CONSTRUCTS_FIXTURE.read_text())["membership"]
    counts: dict[tuple[str, str, str], tuple[int, str]] = {}
    for row in membership["shorthand"]:
        for minor in check.SUPPORTED_PYTHON_MINORS:
            value = check.by_version(row["digest"], minor)
            if isinstance(value, list):
                counts[(row["engine"], minor, row["atom"])] = (int(value[0]), str(value[1]))
    return counts


def _membership_values(
    engine: str, atom: str, domain: DomainTable
) -> tuple[dict[str, object], int | None]:
    from tests.contracts import regex_audit_check as check

    members = tool.basis_members(tool.lex(atom), _engine_consumer(engine))
    counts = _fixture_counts()
    python: dict[str, object] = {}
    for minor in check.SUPPORTED_PYTHON_MINORS:
        if members is None:
            python[minor] = NOT_MEASURED
            continue
        per_basis: dict[str, object] = {}
        for basis in members:
            digest = domain.python_digest.get((engine, minor, basis))
            known = counts.get((engine, minor, basis))
            count = known[0] if known is not None and known[1] == digest else None
            per_basis[basis] = {"count": count, "digest": digest} if digest else NOT_MEASURED
        python[minor] = per_basis
    measured = [text for text in (atom, *(members or ())) if text in domain.rust]
    rust = _points(domain.rust[measured[0]]) if measured else None
    return python, rust


def _differences(engine: str, atom: str, domain: DomainTable) -> dict[str, object]:
    """The code points only the running Python engine matches, and those only Rust matches."""
    from tests.contracts import regex_audit_check as check

    python = domain.python.get((engine, atom))
    rust = domain.rust.get(atom)
    if python is None or rust is None:
        return {"minor": check.RUNNING_MINOR, "python_only": None, "rust_only": None}
    return {
        "minor": check.RUNNING_MINOR,
        "python_only": [list(pair) for pair in check._combine(python, rust, "sub")],
        "rust_only": [list(pair) for pair in check._combine(rust, python, "sub")],
    }


def _engine_consumer(engine: str) -> str:
    return next(c for c in tool.CONSUMER_CLASSES if tool.CONSUMER_ENGINES[c] == engine)


def _decision_row(
    decision: str, rows: Sequence[tuple[FoundRegex, Verdict]], domain: DomainTable | None
) -> dict[str, object]:
    from tests.contracts import regex_audit_check as check

    engine, construct = _construct(decision)
    kind = _decision_kind(decision)
    atoms = _decision_atoms(decision, rows, domain)
    python: dict[str, dict[str, object]] = {minor: {} for minor in check.SUPPORTED_PYTHON_MINORS}
    rust: dict[str, object] = {}
    for atom in atoms:
        if domain is None:
            break
        if kind == "empty-subject":
            for minor in check.SUPPORTED_PYTHON_MINORS:
                offsets = domain.python_boundary.get((engine, minor, atom), {}).get("")
                python[minor][atom] = list(offsets) if offsets is not None else NOT_MEASURED
            offsets = domain.rust_boundary.get(atom, {}).get("")
            rust[atom] = list(offsets) if offsets is not None else NOT_MEASURED
        else:
            values, count = _membership_values(engine, atom, domain)
            for minor, value in values.items():
                python[minor][atom] = value
            rust[atom] = {"count": count}
            if not kind:
                rust[atom] = {"count": count, **_differences(engine, atom, domain)}
    occurrences = sum(decision in verdict.decisions for _, verdict in rows)
    return {
        "id": decision,
        "engine": engine,
        "construct": construct,
        "atoms": atoms,
        "occurrences": occurrences,
        "python": python,
        "rust": rust,
        "options": list(DECISION_OPTIONS[kind]),
    }


def _report_row(item: FoundRegex, verdict: Verdict) -> dict[str, object]:
    flags = tool.CONSUMER_FLAGS[item.consumer]
    return {
        "source": item.source,
        "location": item.location,
        "pattern": item.pattern,
        "consumer": item.consumer,
        "via": item.via,
        "reference": item.reference,
        "verdict": verdict.kind,
        "families": list(verdict.families),
        "python_text": verdict.python_text,
        "rust_text": verdict.rust_text,
        "reason": verdict.reason,
        "atoms": list(verdict.atoms),
        "decisions": list(verdict.decisions),
        "verified": verdict.verified,
        "universal": verdict.universal,
        "flags": {name: name in flags for name in ("case_insensitive", "multi_line")},
        "mode": tool.CONSUMER_MODES[item.consumer],
        "engine": tool.CONSUMER_ENGINES[item.consumer],
        "boundaries": [dict(entry) for entry in verdict.boundaries],
    }


def _cell(value: object) -> str:
    """A Markdown table cell: pipes escaped and newlines shown as ``\\n``; text kept otherwise."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "\\n")


def _code(value: object) -> str:
    return f"`{_cell(value)}`" if value not in (None, "") else ""


def _table(header: Sequence[str], body: Iterator[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return lines


MARKDOWN_CELL_LIMIT = 600


def _capped(value: object) -> str:
    """A JSON value for a Markdown cell, cut at the limit; the JSON report keeps it whole."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= MARKDOWN_CELL_LIMIT:
        return text
    return text[:MARKDOWN_CELL_LIMIT] + " ... (complete in regex-audit.json)"


def _summary(report_rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    def counted(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(row[key]) for row in report_rows).items()))

    return {
        "rows": len(report_rows),
        "verdicts": counted("verdict"),
        "reasons": counted("reason"),
        "consumers": counted("consumer"),
    }


def _markdown(report: Mapping[str, Any]) -> str:
    rows = report["rows"]
    lines = [
        "# Regex audit report",
        "",
        "Generated by tests/contracts/test_regex_audit.py in record mode; not an asserted fixture.",
        "",
        "## Summary",
        "",
        f"{report['summary']['rows']} patterns.",
        "",
    ]
    for key in ("verdicts", "reasons", "consumers"):
        lines += [f"- {key}: " + ", ".join(f"{k} {v}" for k, v in report["summary"][key].items())]
    lines += ["", "## Patterns", ""]
    lines += _table(
        ("source", "location", "pattern", "consumer", "verdict", "rust text", "reason", "verified"),
        (
            (
                _cell(row["source"]),
                _cell(row["location"]),
                _code(row["pattern"]),
                row["consumer"],
                row["verdict"],
                _code(row["rust_text"] or row["python_text"])
                + (
                    " (unverified proposal)"
                    if row["verdict"] == "reported" and row["python_text"]
                    else ""
                ),
                _cell(row["reason"]),
                str(row["verified"]).lower(),
            )
            for row in rows
        ),
    )
    lines += ["", "## Semantic decisions", ""]
    lines += _table(
        ("id", "atoms", "occurrences", "python", "rust", "options"),
        (
            (
                row["id"],
                ", ".join(_code(atom) for atom in row["atoms"]),
                str(row["occurrences"]),
                _cell(_capped(row["python"])),
                _cell(_capped(row["rust"])),
                _cell("; ".join(row["options"])),
            )
            for row in report["decisions"]
        ),
    )
    return "\n".join(lines) + "\n"


def _why(row: Mapping[str, Any]) -> str:
    reason = str(row["reason"])
    if reason.startswith("semantic-decision:"):
        return f"{reason}: the engines differ on a construct no operator ruling has accepted"
    return reason


def _options(row: Mapping[str, Any]) -> str:
    reason = str(row["reason"])
    if reason.startswith("semantic-decision:"):
        decision = reason.removeprefix("semantic-decision:")
        return "; ".join(DECISION_OPTIONS[_decision_kind(decision)])
    return REASON_OPTIONS.get(reason, DEFAULT_OPTIONS)


def _questions(report: Mapping[str, Any]) -> str:
    reported = [row for row in report["rows"] if row["verdict"] == "reported"]
    lines = [
        "# Regex operator questions",
        "",
        "Generated by tests/contracts/test_regex_audit.py in record mode. One row per reported",
        "pattern; nothing here is refused or rewritten in src/.",
        "",
    ]
    lines += _table(
        ("file", "key", "pattern", "consumer class", "why no linear-time rewrite", "options"),
        (
            (
                _cell(row["source"]),
                _cell(row["location"]),
                _code(row["pattern"]),
                row["consumer"],
                _cell(_why(row)),
                _cell(_options(row)),
            )
            for row in reported
        ),
    )
    lines += ["", "## Fixed operator items", ""]
    lines += [f"{index}. {_cell(text)}" for index, text in enumerate(FIXED_ITEMS, 1)]
    return "\n".join(lines) + "\n"


def write_report(
    rows: Sequence[tuple[FoundRegex, Verdict]],
    json_path: Path,
    md_path: Path,
    questions_path: Path | None,
    *,
    domain: DomainTable | None = None,
) -> None:
    """Write the JSON report, the Markdown report and the operator questions file."""
    ordered = sorted(rows, key=lambda pair: (pair[0].source, pair[0].location, pair[0].pattern))
    report_rows = [_report_row(item, verdict) for item, verdict in ordered]
    decision_ids = sorted({d for _, verdict in ordered for d in verdict.decisions})
    report = {
        "schema_version": 1,
        "summary": _summary(report_rows),
        "rows": report_rows,
        "decisions": [_decision_row(d, ordered, domain) for d in decision_ids],
    }
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(_markdown(report))
    if questions_path is not None:
        questions_path.write_text(_questions(report))
