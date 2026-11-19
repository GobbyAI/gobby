"""Adapter corpus contracts: manifest validation, coverage, replay and the special branches.

The committed corpus is read through ``adapters_corpus``. Negative coverage checks copy it to
``tmp_path`` and remove one required item. Every required set is a constant in this file, so
editing the committed JSON cannot relax a contract. Each case count floor is asserted before
any case is indexed or copied.

A case must change outcome when the production path it exercises is broken. The path is
derived here from the case's kind and source (and, for a node-origin case, from a constant
table keyed by its branch), never read from the case, so a case cannot name a path it does
not reach. ``replay_case`` builds its adapter (or app) afresh on every call, so a class or
module attribute patched before the call is the one the replay runs.

A node-origin case carries no file, so the node path and the hub path return the same value
for it. Its reach check therefore watches the hub-side read instead of the return value: with
the branch's reader broken, the node replay must still match (the skip really skips), and with
``node_origin`` also forced false the outcome must change (the payload really sits on the
branch that reads).
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from gobby.adapters import claude_code, droid, grok
from gobby.adapters.agy import AgyAdapter
from gobby.adapters.capabilities import get_provider_capabilities
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl import hooks_adapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.git import GitAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.servers.routes.mcp.hooks import SUPPORTED_HOOK_SOURCES
from gobby.sessions.transcripts import agy as agy_transcripts
from tests.contracts import adapters_corpus

CONTRACTS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = CONTRACTS_DIR / "adapters"
MIN_CASES = 18
# Each hook-translating adapter, keyed by the source name its cases carry. The git source has
# its own verdict cases and no response-field table, so it is not in this map.
ADAPTER_CLASSES: dict[str, Any] = {
    "claude": ClaudeCodeAdapter,
    "codex": CodexHooksAdapter,
    "droid": DroidAdapter,
    "grok": GrokAdapter,
    "agy": AgyAdapter,
}
CLAUDE = "claude"
CASE_KINDS = ("inbound", "outbound", "node_origin", "git_verdict", "route_refusal")
# Response fields the adapter layer sets, not the rule engine. No declared capability row lists
# modify_args, so each of these needs an outbound case of its own, whatever the source.
ADAPTER_OWNED_RESPONSE_FIELDS = ("system_message", "modify_args")
GIT_VERDICTS = ("allow", "block")
ROUTE_REFUSALS = ("unknown_source", "malformed_payload")
REFUSAL_STATUS = 400
TRANSLATION_METHODS = ("translate_to_hook_event", "translate_from_hook_response")
# Text no recorded adapter output contains, so a stubbed path cannot pass as recorded output.
BROKEN_PATH = "corpus reach check: production path broken"


class _PathBrokenError(BaseException):
    """Raised by a production path the reach checks have stubbed out.

    It derives from ``BaseException`` so no ``except Exception`` or ``except OSError`` handler
    in an adapter can swallow it and hide that the stubbed path ran.
    """


def _broken(*_args: object, **_kwargs: object) -> NoReturn:
    raise _PathBrokenError(BROKEN_PATH)


def _not_node_origin() -> bool:
    return False


class _UnreadablePath(Path):
    """A ``Path`` whose file reads fail, bound in place of a module's ``Path`` name."""

    def open(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise _PathBrokenError(BROKEN_PATH)

    def read_text(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise _PathBrokenError(BROKEN_PATH)

    def read_bytes(self) -> NoReturn:
        raise _PathBrokenError(BROKEN_PATH)


# Node-origin branches that skip a hub-side file read, keyed by (source, branch), each mapped to
# the ``(owner, attribute, stub)`` that breaks the read the hub path would make. The spill reader
# exists in three adapters, so each one needs its own case.
NODE_ORIGIN_READERS: dict[tuple[str, str], tuple[object, str, Callable[..., object]]] = {
    ("claude", "spill_reader_skip"): (claude_code, "Path", _UnreadablePath),
    ("grok", "spill_reader_skip"): (grok, "Path", _UnreadablePath),
    ("codex", "spill_reader_skip"): (hooks_adapter, "Path", _UnreadablePath),
    ("droid", "notification_wait_skip"): (droid, "droid_transcript_has_tool_id", _broken),
    ("droid", "idle_prompt_cancel_skip"): (
        droid,
        "droid_transcript_confirms_turn_cancellation",
        _broken,
    ),
    ("agy", "prompt_backfill_skip"): (agy_transcripts, "read_current_user_prompt", _broken),
}
NODE_ORIGIN_BRANCHES = tuple(NODE_ORIGIN_READERS)


def _outcome(case: dict[str, Any]) -> object:
    try:
        return adapters_corpus.replay_case(case)
    except _PathBrokenError:
        return BROKEN_PATH


def _exercised_paths(case: dict[str, Any]) -> list[tuple[object, str, Callable[..., object]]]:
    """Return the ``(owner, attribute, stub)`` triples that break the path a case names."""
    kind = case.get("kind")
    if kind == "git_verdict":
        return [(GitAdapter, "translate_from_hook_response", _broken)]
    adapter = ADAPTER_CLASSES.get(str(case.get("source")))
    if adapter is None:
        return []
    if kind == "inbound":
        return [(adapter, "translate_to_hook_event", _broken)]
    if kind == "outbound":
        return [(adapter, "translate_from_hook_response", _broken)]
    if kind == "node_origin":
        reader = _node_origin_reader(case)
        if reader is None:
            return []
        # node_origin is imported by name into each adapter module; patch that binding.
        return [(inspect.getmodule(adapter), "node_origin", _not_node_origin), reader]
    return []


def _node_origin_reader(case: dict[str, Any]) -> tuple[object, str, Callable[..., object]] | None:
    return NODE_ORIGIN_READERS.get((str(case.get("source")), str(case.get("branch"))))


def _committed_cases() -> list[dict[str, Any]]:
    _manifest, cases = adapters_corpus.load_corpus(CORPUS_DIR)
    return cases


def _declared_response_fields(source: str) -> set[str]:
    capabilities = get_provider_capabilities(source)
    fields: set[str] = set()
    for hook in capabilities.hook_events.values():
        fields |= set(hook.supported_response_fields)
    return fields


def _write_corpus(tmp_path: Path, manifest: dict[str, Any], cases: list[dict[str, Any]]) -> Path:
    corpus = tmp_path / "adapters"
    (corpus / "cases").mkdir(parents=True)
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    (corpus / "cases" / "synthetic.json").write_text(json.dumps({"cases": cases}))
    return corpus


def _synthetic_case(case_id: str, kind: str, source: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "kind": kind,
        "source": source,
        "provenance": "synthetic",
        "hook_type": "synthetic-hook",
        "payload": {},
        "expect": {},
    }


def _manifest_offender(offender: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Return ``(manifest, cases, needle)``: a corpus whose one defect names ``needle``."""
    manifest: dict[str, Any] = {
        "schema_version": adapters_corpus.SCHEMA_VERSION,
        "sources": [CLAUDE],
        "case_kinds": list(CASE_KINDS),
    }
    if offender == "unknown_source":
        case = _synthetic_case("unknown-source", "inbound", "no-such-source")
        return manifest, [case], "no-such-source"
    if offender == "stale_schema_version":
        return {**manifest, "schema_version": 0}, [], "schema_version"
    return manifest, [_synthetic_case("unknown-kind", "no-such-kind", CLAUDE)], "no-such-kind"


def _carries_item(case: dict[str, Any], axis: str, item: str) -> bool:
    if axis == "hook_type":
        return (
            case.get("kind") == "inbound"
            and case.get("source") == CLAUDE
            and case.get("hook_type") == item
        )
    return case.get("kind") == "outbound" and item in case.get("response", {})


def _drop_items(corpus: Path, axis: str, item: str) -> None:
    for path in sorted((corpus / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        document["cases"] = [
            case for case in document["cases"] if not _carries_item(case, axis, item)
        ]
        path.write_text(json.dumps(document))


def _refusal_cases() -> dict[str, dict[str, Any]]:
    refusals = {
        case.get("refusal"): case
        for case in _committed_cases()
        if case.get("kind") == "route_refusal"
    }
    missing = [name for name in ROUTE_REFUSALS if name not in refusals]
    assert not missing, f"no route refusal case for: {missing}"
    return {name: refusals[name] for name in ROUTE_REFUSALS}


@pytest.mark.parametrize(
    "offender", ("unknown_source", "stale_schema_version", "unknown_case_kind")
)
def test_adapters_manifest_contract(tmp_path: Path, offender: str) -> None:
    manifest, cases, needle = _manifest_offender(offender)
    corpus = _write_corpus(tmp_path, manifest, cases)
    with pytest.raises(ValueError, match=re.escape(needle)):
        adapters_corpus.load_corpus(corpus)


def test_adapters_corpus_covers_hook_types_and_fields() -> None:
    cases = _committed_cases()
    gaps: list[str] = []
    for source, adapter in ADAPTER_CLASSES.items():
        inbound = {
            case.get("hook_type")
            for case in cases
            if case.get("kind") == "inbound" and case.get("source") == source
        }
        gaps += [
            f"{source} inbound {hook}" for hook in sorted(adapter.EVENT_MAP) if hook not in inbound
        ]
        outbound: set[str] = set()
        for case in cases:
            if case.get("kind") == "outbound" and case.get("source") == source:
                outbound |= set(case.get("response", {}))
        gaps += [
            f"{source} outbound {field}"
            for field in sorted(_declared_response_fields(source) - outbound)
        ]
    assert not gaps, f"adapter corpus lacks cases for: {gaps}"


def test_adapters_corpus_sets_adapter_owned_response_fields() -> None:
    outbound = [case for case in _committed_cases() if case.get("kind") == "outbound"]
    unset = [
        field
        for field in ADAPTER_OWNED_RESPONSE_FIELDS
        if not any(field in case.get("response", {}) for case in outbound)
    ]
    assert not unset, f"no outbound adapter case sets: {unset}"


def test_python_adapters_replay_corpus() -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"adapter corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    mismatched = [
        case["id"] for case in cases if adapters_corpus.replay_case(case) != case["expect"]
    ]
    assert not mismatched, f"adapter cases that do not replay: {mismatched}"


def test_adapter_cases_change_outcome_when_their_adapter_path_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [case for case in _committed_cases() if case.get("kind") != "route_refusal"]
    assert len(cases) >= MIN_CASES, (
        f"adapter corpus holds {len(cases)} adapter-call cases; at least {MIN_CASES} required"
    )
    unreached: list[str] = []
    for case in cases:
        paths = _exercised_paths(case)
        if not paths:
            unreached.append(f"{case['id']}: kind and source name no adapter path")
            continue
        reader = _node_origin_reader(case) if case.get("kind") == "node_origin" else None
        if reader is not None:
            with monkeypatch.context() as patch:
                owner, attribute, stub = reader
                patch.setattr(owner, attribute, stub)
                if _outcome(case) != case["expect"]:
                    unreached.append(f"{case['id']}: the node path still reads {attribute}")
        with monkeypatch.context() as patch:
            for owner, attribute, stub in paths:
                patch.setattr(owner, attribute, stub)
            if _outcome(case) == case["expect"]:
                unreached.append(f"{case['id']}: outcome unchanged with its path broken")
    assert not unreached, f"adapter cases that never reach their adapter path: {unreached}"


def test_adapter_cases_start_from_native_provider_payloads() -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"adapter corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    not_native: list[str] = []
    for case in cases:
        if case.get("kind") not in ("inbound", "node_origin"):
            continue
        adapter = ADAPTER_CLASSES.get(str(case.get("source")))
        if adapter is None or case.get("hook_type") not in adapter.EVENT_MAP:
            not_native.append(f"{case['id']}: hook type is not a native hook name")
        if not case.get("payload"):
            not_native.append(f"{case['id']}: empty payload")
    assert not not_native, f"adapter cases without a native provider payload: {not_native}"


def test_adapters_corpus_has_node_origin_cases() -> None:
    node_cases = [case for case in _committed_cases() if case.get("kind") == "node_origin"]
    branches = {(case.get("source"), case.get("branch")) for case in node_cases}
    missing = [pair for pair in NODE_ORIGIN_BRANCHES if pair not in branches]
    assert not missing, f"no node-origin case for (source, branch): {missing}"
    with_file = [case["id"] for case in node_cases if case.get("file_present") is not False]
    assert not with_file, f"node-origin cases that carry a file: {with_file}"


def test_git_source_covers_event_and_both_verdicts() -> None:
    git_cases = [
        case
        for case in _committed_cases()
        if case.get("kind") == "git_verdict" and case.get("source") == "git"
    ]
    events = {case.get("hook_type") for case in git_cases}
    assert len(events) == 1, (
        f"git verdict cases must share one event, found {sorted(map(str, events))}"
    )
    verdicts = {case.get("verdict") for case in git_cases}
    missing = [verdict for verdict in GIT_VERDICTS if verdict not in verdicts]
    assert not missing, f"git source lacks verdict(s): {missing}"


def test_unknown_source_and_malformed_payload_refused() -> None:
    refusals = _refusal_cases()
    for name, case in refusals.items():
        expect = case["expect"]
        assert expect.get("status") == REFUSAL_STATUS, f"{name} status: {expect.get('status')!r}"
        assert expect.get("adapter_called") is False, f"{name} must refuse before an adapter call"
    named = set(refusals["unknown_source"]["expect"].get("supported_sources_named", ()))
    assert named == set(SUPPORTED_HOOK_SOURCES), f"unknown-source message names {sorted(named)}"


def test_route_refusals_never_reach_an_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    refusals = _refusal_cases()
    for adapter in (*ADAPTER_CLASSES.values(), GitAdapter):
        for method in TRANSLATION_METHODS:
            monkeypatch.setattr(adapter, method, _broken)
    reached = [name for name, case in refusals.items() if _outcome(case) != case["expect"]]
    assert not reached, f"route refusals whose outcome depends on an adapter: {reached}"


@pytest.mark.parametrize(
    ("axis", "item"),
    (
        ("hook_type", sorted(ClaudeCodeAdapter.EVENT_MAP)[0]),
        ("response_field", "system_message"),
        ("response_field", "modify_args"),
    ),
)
def test_adapters_coverage_negative(tmp_path: Path, axis: str, item: str) -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"adapter corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    corpus = tmp_path / "adapters"
    shutil.copytree(CORPUS_DIR, corpus)
    _drop_items(corpus, axis, item)
    with pytest.raises(ValueError, match=re.escape(item)):
        adapters_corpus.check_coverage(corpus, axis)
