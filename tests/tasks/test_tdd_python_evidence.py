"""Receipt-backed Python shape edits must preserve genuine TDD chronology."""

import ast
import json
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.storage.session_models import Session
from gobby.tasks import transcript_evidence
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.tdd_python_evidence import PythonBindingCache, _binding_count
from gobby.tasks.transcript_evidence import (
    _derive_transcript_evidence_sync,
    _derive_transcript_path_evidence,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_evidence_snapshots import (
    EvidenceSnapshot,
    load_durable_snapshot,
    read_transcript_suffix,
)

START = datetime(2026, 10, 3, tzinfo=UTC)


def _bash_checkout_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str | None = None,
    *,
    incremental: bool = False,
) -> tuple[TranscriptEvidence, tuple[AcceptanceTest, ...]]:
    """Replay retained tool/output bytes in an isolated, relocated Git repository."""
    from tests.tasks.test_tdd_retained_output import START as RECALL_START
    from tests.tasks.test_tdd_retained_output import _recall_store

    fixtures = Path(__file__).parent / "fixtures"
    fixture = json.loads((fixtures / "bash_checkout_retained_cycle.json").read_text())
    product = "src/gobby/servers/routes/nodes.py"
    test_path = "tests/servers/test_node_routes.py"
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    git("init", "-q")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    test_source = (fixtures / "bash_checkout_test.txt").read_text()
    for path, source in (
        (test_path, test_source),
        (product, (fixtures / "bash_checkout_restored.txt").read_text()),
    ):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    git("add", ".")
    git("commit", "-qm", "restored fixture tree")
    restored = git("rev-parse", "HEAD")
    (root / product).write_text((fixtures / "bash_checkout_stub.txt").read_text())
    git("add", ".")
    git("commit", "-qm", "committed stub fixture tree")
    stub = git("rev-parse", "HEAD")
    replacements = {
        fixture["root"]: str(root),
        "485b7c140e": "f" * 40 if invalid == "missing_object" else restored,
        "08f161662aefa541d18a54eb36defcbead34ee3d": stub,
    }

    def relocated(text: str) -> str:
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    records: list[dict[str, Any]] = []

    def pair(call: dict[str, Any]) -> None:
        records.extend(
            [
                {
                    "type": "assistant",
                    "timestamp": call["started_at"],
                    "cwd": str(root),
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call["id"],
                                "name": call["name"],
                                "input": call["arguments"],
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": call["completed_at"],
                    "cwd": str(root),
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call["id"],
                                **call["result"],
                            }
                        ],
                    },
                },
            ]
        )

    # Synthetic fixture setup; never a claimed historical test-authoring receipt.
    # Product baseline is deliberately absent, as in the unread retained cycle.
    pair(
        {
            "id": "fixture-test-source",
            "name": "Write",
            "started_at": "2026-10-18T18:40:00Z",
            "completed_at": "2026-10-18T18:40:01Z",
            "arguments": {"file_path": str(root / test_path), "content": test_source},
            "result": {
                "content": f"File created successfully at: {root / test_path}",
                "is_error": False,
            },
        }
    )
    for original in fixture["calls"]:
        call = json.loads(relocated(json.dumps(original)))
        if invalid == "failed_restore" and "git checkout " in call["arguments"].get("command", ""):
            call["result"] = {"content": "Exit code 1", "is_error": True}
        if invalid == "dirty_receipt" and call["id"] == "toolu_01WDyj1DGAyxnJNNj2aYqRnB":
            call["result"]["content"] += f"\n M {product}"
        if "git checkout " in call["arguments"].get("command", ""):
            if invalid == "untrusted_suffix":
                call["arguments"]["command"] += " && echo unproven"
            if invalid == "symbolic_ref":
                call["arguments"]["command"] = call["arguments"]["command"].replace(
                    restored, "HEAD~1"
                )
        if invalid == "stub_mismatch" and call["name"] == "Edit":
            call["arguments"]["new_string"] = call["arguments"]["new_string"].replace(
                "raise NotImplementedError", "raise RuntimeError"
            )
        pair(call)
    records.sort(key=lambda row: row["timestamp"])
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    recall = relocated((fixtures / "bash_checkout_recall.txt").read_text())
    _recall_store(
        tmp_path / "home",
        monkeypatch,
        recall,
        created=(datetime(2026, 10, 18, 18, 54, 13, tzinfo=UTC) - RECALL_START).total_seconds(),
        recall="45d9703c9157",
    )
    session = replace(
        _native_session(tmp_path, _native_fixture()),
        source="claude",
        transcript_path=str(transcript),
    )
    files = {test_path, product}
    resume = None
    if incremental:
        cutoff = next(
            call["completed_at"]
            for call in fixture["calls"]
            if call["id"] == "toolu_01WDyj1DGAyxnJNNj2aYqRnB"
        )
        prefix = [row for row in records if row["timestamp"] <= cutoff]
        transcript.write_text("\n".join(json.dumps(row) for row in prefix) + "\n")
        _, resume = _derive_transcript_path_evidence(
            session,
            str(transcript),
            None,
            default_validation_detection_config(),
            files,
            str(root),
            frozenset((str(root), path) for path in files),
            [str(transcript)],
            resume=None,
        )
        transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    evidence, snapshot = _derive_transcript_path_evidence(
        session,
        str(transcript),
        None,
        default_validation_detection_config(),
        files,
        str(root),
        frozenset((str(root), path) for path in files),
        [str(transcript)],
        resume=resume,
    )
    if incremental:
        assert snapshot is not None
        assert snapshot.parsed_from_offset > 0
    symbols = [
        node.name
        for node in ast.parse(test_source).body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and node.name != "test_out_of_order_duplicate_exit_is_charged_once"
    ]
    tests = tuple(
        AcceptanceTest(
            reference=f"{test_path}::{symbol}", path=test_path, symbol=symbol, body=test_source
        )
        for symbol in symbols
    )
    return evidence, tests


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "resumed"])
def test_bash_git_checkout_restore_credits_retained_stub_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incremental: bool
) -> None:
    from gobby.tasks.tdd_pytest_provenance import recalled_pytest_sections
    from gobby.tasks.tdd_retained_output import delivered_proof_runs

    evidence, tests = _bash_checkout_cycle(tmp_path, monkeypatch, incremental=incremental)
    assert len(tests) == 9
    restored = [edit for edit in evidence.edits if edit.tool_name == "Bash"]
    assert len(restored) == 1, [
        (edit.path, edit.tool_name, edit.source_after is not None, edit.source_confirmed)
        for edit in evidence.edits
    ]
    assert restored[0].source_confirmed
    assert (
        restored[0].source_after
        == (Path(__file__).parent / "fixtures/bash_checkout_restored.txt").read_text()
    )
    red = next(run for run in evidence.validation_runs if run.outcome == "failure")
    proofs = list(delivered_proof_runs(red, restored[0].timestamp))
    assert len(proofs) == 1
    proof = proofs[0]
    assert proof.output_recovered_from == "rtk recall 45d9703c9157"
    assert len(recalled_pytest_sections(proof.output or "", evidence, proof)) == 18
    for test in tests:
        result = evaluate_tdd_evidence((test,), evidence)
        assert result.passed, (test.reference, result.findings)
        assert len(result.red_runs) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_object",
        "failed_restore",
        "dirty_receipt",
        "untrusted_suffix",
        "symbolic_ref",
        "stub_mismatch",
    ],
)
def test_unprovable_bash_restore_earns_no_stub_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    evidence, tests = _bash_checkout_cycle(tmp_path, monkeypatch, invalid)
    result = evaluate_tdd_evidence(tests, evidence)
    assert not result.passed
    assert result.red_runs == ()


_NATIVE_ROOT = "/Users/josh/.gobby/worktrees/gobby/lane-5-hub-mode"
_NATIVE_PRODUCT = "src/gobby/hub/caller_scope.py"
_NATIVE_TEST = "tests/hub/test_caller_scope.py"


def _native_fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            Path(__file__).with_name("fixtures").joinpath("grok_native_creation.json").read_text()
        ),
    )


def _native_updates(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-envelope stored tool bytes for the real Grok parser; never execute them."""
    call_id = call.get("id") or call["tool_call_id"]
    result = call["result"]
    if "rawOutput" not in result:
        applied = {
            **call["arguments"],
            "absolute_path": call["arguments"]["file_path"],
            "tool_output_for_prompt": result["tool_output_for_prompt"],
        }
        result = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": result["status"],
            "content": result.get("diffs", [result.get("diff")]),
            "rawOutput": {"type": "SearchReplace", "EditsApplied": applied},
        }
    stamp = datetime.fromisoformat(call["timestamp"])
    return [
        {
            "timestamp": stamp.isoformat(),
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": call["tool_name"],
                "rawInput": call["arguments"],
            },
        },
        {
            # Stored rendered messages retain the call time. A small isolated
            # completion offset preserves ordering without inventing live proof.
            "timestamp": (stamp + timedelta(microseconds=1)).isoformat(),
            "update": result,
        },
    ]


def _native_session(
    tmp_path: Path, fixture: dict[str, Any], extra: list[dict[str, Any]] | None = None
) -> Session:
    records = [
        record
        for call in [
            *fixture["creations"],
            *fixture["partials"],
            *fixture["runs"],
            fixture["implementation"],
            fixture["green"],
        ]
        for record in _native_updates(call)
    ]
    records.extend(extra or [])
    records.sort(key=lambda row: row["timestamp"])
    transcript = tmp_path / "grok.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return Session(
        id="owner",
        external_id="native-fixture",
        machine_id="isolated",
        source="grok",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )


def _native_evidence(
    tmp_path: Path, fixture: dict[str, Any], extra: list[dict[str, Any]] | None = None
) -> TranscriptEvidence:
    session = _native_session(tmp_path, fixture, extra)
    transcript = str(session.transcript_path)
    files = {_NATIVE_PRODUCT, _NATIVE_TEST, "tests/servers/test_caller_scope_binding.py"}
    evidence, _ = _derive_transcript_path_evidence(
        session,
        str(transcript),
        None,
        default_validation_detection_config(),
        files,
        _NATIVE_ROOT,
        frozenset((_NATIVE_ROOT, path) for path in files),
        [str(transcript)],
        resume=None,
    )
    return evidence


def _native_cached_evidence(
    session: Session, resume: EvidenceSnapshot | None = None
) -> tuple[TranscriptEvidence, EvidenceSnapshot | None]:
    files = {_NATIVE_PRODUCT, _NATIVE_TEST, "tests/servers/test_caller_scope_binding.py"}
    return _derive_transcript_evidence_sync(
        session,
        None,
        default_validation_detection_config(),
        files,
        _NATIVE_ROOT,
        frozenset((_NATIVE_ROOT, path) for path in files),
        None,
        "isolated",
        resume,
    )


def _native_acceptance(fixture: dict[str, Any]) -> tuple[AcceptanceTest, ...]:
    return tuple(
        AcceptanceTest(
            reference=f"{_NATIVE_TEST}::{symbol}",
            path=_NATIVE_TEST,
            symbol=symbol,
            body=fixture["creations"][1]["arguments"]["new_string"],
        )
        for symbol in ("test_require_machine_owned", "test_binding_reaches_child_tasks")
    )


def test_native_searchreplace_creation_seeds_snapshot_for_red(tmp_path: Path) -> None:
    fixture = _native_fixture()
    evidence = _native_evidence(tmp_path, fixture)
    result = evaluate_tdd_evidence(_native_acceptance(fixture), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (fixture["runs"][0]["arguments"]["command"],)
    created = next(edit for edit in evidence.edits if edit.path == _NATIVE_PRODUCT)
    assert created.source_after == fixture["creations"][0]["arguments"]["new_string"]
    assert created.source_created and created.source_confirmed
    # Original binding partials have no authenticated full baseline: no invention.
    binding = [edit for edit in evidence.edits if edit.path.startswith("tests/servers/")]
    assert len(binding) == 8
    assert all(edit.source_after is None for edit in binding)


@pytest.mark.parametrize("durable", [False, True], ids=["memory", "durable"])
@pytest.mark.parametrize("append_suffix", [False, True], ids=["eof", "appended"])
def test_native_creation_rederives_pre_fix_v43_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable: bool, append_suffix: bool
) -> None:
    """A valid old cache must not hide authenticated historical creation records."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    fixture = _native_fixture()
    session = _native_session(tmp_path, fixture)
    original_dumps = json.dumps

    def legacy_dumps(value: Any, **kwargs: Any) -> str:
        if isinstance(value, dict) and "derivation_version" in value:
            value = {**value, "derivation_version": 43}
        return original_dumps(value, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(json, "dumps", legacy_dumps)
        legacy.setattr(transcript_evidence, "native_created_source", lambda *args: None)
        stale_evidence, stale = _native_cached_evidence(session)

    assert stale is not None
    stale_created = next(edit for edit in stale.edits if edit.path == _NATIVE_PRODUCT)
    assert stale_created.source_after is None and not stale_created.source_created
    assert not evaluate_tdd_evidence(_native_acceptance(fixture), stale_evidence).passed
    transcript = Path(str(session.transcript_path))
    assert stale.watermark == transcript.stat().st_size
    suffix = read_transcript_suffix(str(transcript), stale)
    assert suffix is not None and suffix.lines == []
    stored = load_durable_snapshot(f"{session.id}:{stale.fingerprint}")
    assert stored is not None and stored.checkpoint() == stale.checkpoint()
    assert stored.edits == stale.edits

    if append_suffix:
        with transcript.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-10-18T00:00:00Z",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Harmless fixture suffix."},
                        },
                    }
                )
                + "\n"
            )

    evidence, refreshed = _native_cached_evidence(session, None if durable else stale)
    assert refreshed is not None
    assert refreshed.parsed_from_offset == 0
    assert refreshed.fingerprint != stale.fingerprint
    assert refreshed.watermark == transcript.stat().st_size
    created = next(edit for edit in evidence.edits if edit.path == _NATIVE_PRODUCT)
    assert created.source_after == fixture["creations"][0]["arguments"]["new_string"]
    assert created.source_created and created.source_confirmed
    acceptance = _native_acceptance(fixture)
    assert len(acceptance) == 2
    result = evaluate_tdd_evidence(acceptance, evidence)
    assert result.passed, result.findings
    assert result.red_runs == (fixture["runs"][0]["arguments"]["command"],)
    binding = [edit for edit in evidence.edits if edit.path.startswith("tests/servers/")]
    assert len(binding) == 8
    assert all(edit.source_after is None for edit in binding)


@pytest.mark.parametrize(
    "invalid",
    ["not-created", "pending", "denied", "append", "mismatched-bytes", "wrong-path", "ambiguous"],
)
def test_native_creation_snapshot_negatives_fail_closed(tmp_path: Path, invalid: str) -> None:
    fixture = _native_fixture()
    creation = fixture["creations"][0]
    receipt = creation["result"]["rawOutput"]["EditsApplied"]
    extra: list[dict[str, Any]] = []
    if invalid == "not-created":
        receipt["tool_output_for_prompt"] = receipt["tool_output_for_prompt"].replace(
            "created", "updated"
        )
    elif invalid == "pending":
        creation["result"]["status"] = "pending"
    elif invalid == "denied":
        creation["result"]["status"] = "failed"
        creation["result"]["error"] = "Permission denied"
    elif invalid == "append":
        creation["result"]["content"][0]["oldText"] = "# existing file\n"
    elif invalid == "mismatched-bytes":
        receipt["new_string"] += "# bytes not supplied\n"
    elif invalid == "wrong-path":
        receipt["absolute_path"] += ".other"
    else:
        # Synthetic adversarial edit, separate from the immutable original calls.
        extra = _native_updates(
            {
                "id": "ambiguous",
                "timestamp": "2026-10-17T04:43:00+00:00",
                "tool_name": "search_replace",
                "arguments": {
                    "file_path": f"{_NATIVE_ROOT}/{_NATIVE_PRODUCT}",
                    "old_string": "raise NotImplementedError",
                    "new_string": "raise RuntimeError",
                },
                "result": {"status": "completed", "tool_output_for_prompt": "updated"},
            }
        )
    evidence = _native_evidence(tmp_path, fixture, extra)
    result = evaluate_tdd_evidence(_native_acceptance(fixture), evidence)

    assert not result.passed
    assert result.red_runs == ()
    red = evidence.validation_runs[0]
    latest = max(
        (
            edit
            for edit in evidence.edits
            if edit.path == _NATIVE_PRODUCT and edit.order < red.order
        ),
        key=lambda edit: edit.order,
        default=None,
    )
    assert latest is None or latest.source_after is None


def test_native_creation_unique_anchors_preserve_full_snapshot(tmp_path: Path) -> None:
    fixture = _native_fixture()
    # Synthetic token-neutral anchored edit checks replay from a proved baseline.
    original = fixture["creations"][0]["arguments"]["new_string"]
    old = "from __future__ import annotations"
    new = old + "  # anchored"
    extra = _native_updates(
        {
            "id": "unique",
            "timestamp": "2026-10-17T02:09:10.500000+00:00",
            "tool_name": "search_replace",
            "arguments": {
                "file_path": f"{_NATIVE_ROOT}/{_NATIVE_PRODUCT}",
                "old_string": old,
                "new_string": new,
            },
            "result": {"status": "completed", "tool_output_for_prompt": "updated"},
        }
    )
    evidence = _native_evidence(tmp_path, fixture, extra)
    snapshots = [edit for edit in evidence.edits if edit.path == _NATIVE_PRODUCT]
    assert snapshots[1].source_after == original.replace(old, new)
    assert snapshots[1].source_confirmed
    assert evaluate_tdd_evidence(_native_acceptance(fixture), evidence).passed


def test_binding_index_preserves_shadowing_with_one_module_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ast.parse(
        "from feature import left, right\n"
        "import feature.tools as tools\n"
        "def nested(left):\n    right = tools()\n"
    )
    walk = ast.walk
    walked: list[ast.AST] = []

    def counted_walk(node: ast.AST) -> Iterator[ast.AST]:
        walked.append(node)
        return walk(node)

    monkeypatch.setattr(ast, "walk", counted_walk)
    cache: PythonBindingCache = {}
    assert _binding_count(module, "left", cache) == 2
    assert _binding_count(module, "right", cache) == 2
    assert _binding_count(module, "tools", cache) == 1
    assert _binding_count(module, "nested", cache) == 1
    assert _binding_count(module, "missing", cache) == 0
    assert walked == [module]


def test_tdd_module_parses_are_bounded_by_distinct_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "from bridge import Bridge\ndef test_feature():\n    assert Bridge().value == 1\n"
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=source,
    )
    bridge = (
        "from stub import Stub\nclass Bridge:\n"
        "    def __init__(self):\n        self.value = existing()\n"
    )
    edits = (
        _edit(test.path, 1, source),
        _edit("src/stub.py", 2, "class Stub:\n    pass\n"),
        _edit("src/other.py", 3, "class Other:\n    pass\n"),
        _edit("src/bridge.py", 4, bridge),
        _edit(test.path, 5, source),
        _edit("src/stub.py", 6, "class Stub:\n    pass\n"),
        _edit(test.path, 7, source),
        _edit("src/bridge.py", 8, bridge),
        _edit("src/bridge.py", 12, bridge),
        _edit("src/bridge.py", 14, bridge),
    )
    runs = tuple(
        replace(
            _run(test, order, red=True),
            output="FAILED tests/test_feature.py::test_feature - AssertionError\nE assert 0 == 1",
        )
        for order in (9, 10, 11)
    )
    evidence = TranscriptEvidence(edits=edits, validation_runs=runs)
    counts: Counter[str] = Counter()
    parse = ast.parse

    def counted_parse(source: str, *args: object, **kwargs: object) -> ast.Module:
        counts[source] += 1
        return parse(source)

    monkeypatch.setattr(ast, "parse", counted_parse)
    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert counts[source] == 1
    assert counts[bridge] == 1
    assert max(counts.values()) == 1
    first_counts = counts.copy()
    assert evaluate_tdd_evidence((test,), evidence) == result
    assert counts == Counter({text: count * 2 for text, count in first_counts.items()})


def _edit(path: str, order: int, source: str | None = None) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="owner",
        source="codex",
        path=path,
        timestamp=START + timedelta(seconds=order * 10),
        order=order,
        tool_name="Write",
        source_after=source,
        source_confirmed=source is not None,
        source_confirmed_at=START + timedelta(seconds=order * 10 + 1),
    )


def _run(test: AcceptanceTest, order: int, *, red: bool) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="owner",
        source="codex",
        command=f"pytest {test.reference} -q",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if red else "success",
        started_at=START + timedelta(seconds=order * 10),
        completed_at=START + timedelta(seconds=order * 10 + 2),
        order=order,
        exit_code=1 if red else 0,
        output=(
            "_______________________ test_feature _______________________\n"
            "E       Failed: DID NOT RAISE <class 'feature.CronSessionError'>\n"
            "tests/test_feature.py:7: Failed\n"
            "FAILED tests/test_feature.py::test_feature\n"
            if red
            else "1 passed"
        ),
    )


def _exception_cycle(
    declaration: str, module_path: str = "src/feature.py"
) -> tuple[AcceptanceTest, TranscriptEvidence]:
    source = (
        "import pytest\n"
        "from feature import CronSessionError, run\n\n"
        "def test_feature():\n"
        "    with pytest.raises(CronSessionError):\n"
        "        run()\n"
    )
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=source,
    )
    stub = replace(
        _edit(module_path, 2, declaration + "\ndef run():\n    return None\n"),
        tool_name="Edit",
        python_added_source=declaration,
    )
    return test, TranscriptEvidence(
        edits=(_edit(test.path, 1, source), stub, _edit(module_path, 4)),
        validation_runs=(_run(test, 3, red=True), _run(test, 5, red=False)),
    )


@pytest.mark.parametrize("base", ["RuntimeError", "Exception", "ValueError"])
def test_exception_declaration_stub_red(base: str) -> None:
    test, evidence = _exception_cycle(f"class CronSessionError({base}):\n    pass\n")

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (f"pytest {test.reference} -q",)


def test_package_export_stub_red() -> None:
    test, evidence = _exception_cycle(
        "class CronSessionError(RuntimeError):\n    pass\n", "src/feature/__init__.py"
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (f"pytest {test.reference} -q",)


@pytest.mark.parametrize(
    "declaration",
    [
        "class CronSessionError(CustomError):\n    pass\n",
        "class CronSessionError(make_base()):\n    pass\n",
        "class CronSessionError(RuntimeError, metaclass=Meta):\n    pass\n",
        "class CronSessionError(RuntimeError):\n    registry.append('side effect')\n",
        "class CronSessionError(RuntimeError):\n    def __str__(self):\n        return 'feature'\n",
        "RuntimeError = CustomError\nclass CronSessionError(RuntimeError):\n    pass\n",
    ],
)
def test_behavioral_exception_declaration_does_not_credit_red(declaration: str) -> None:
    test, evidence = _exception_cycle(declaration)

    result = evaluate_tdd_evidence((test,), evidence)

    assert not result.passed
    assert not result.red_runs


def test_unconfirmed_exception_declaration_does_not_credit_red() -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(evidence.edits[1], source_confirmed=False, source_confirmed_at=None)

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


def test_exception_base_shadowed_outside_added_fragment() -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(
        evidence.edits[1],
        source_after="RuntimeError = CustomError\n" + (evidence.edits[1].source_after or ""),
    )

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


@pytest.mark.parametrize(
    "behavior",
    [
        "raise CronSessionError",
        "raise CronSessionError('active')",
        "error = CronSessionError('active')\n    raise error",
        "raise feature.CronSessionError('active')",
        None,
    ],
)
def test_exception_already_raised_in_confirmed_module_is_not_stub(behavior: str | None) -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(
        evidence.edits[1],
        source_after=(
            f"class CronSessionError(RuntimeError):\n    pass\n\ndef run():\n    {behavior}\n"
        )
        if behavior is not None
        else None,
    )

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


def _move_cycle(module_path: str = "src/feature.py") -> tuple[AcceptanceTest, TranscriptEvidence]:
    source = "from feature import payload\n\ndef test_feature():\n    assert payload()['seat']\n"
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=source,
    )
    moved = "def payload():\n    return {'kind': 'base'}\n"
    before_test = replace(_edit(module_path, 0, moved), source_created=True)
    repeated_write = _edit(module_path, 2, moved)
    cleanup = replace(
        _edit("src/old.py", 3),
        tool_name="Edit",
        source_fragment="from collections.abc import Callable\n",
        source_confirmed=True,
    )
    red = replace(
        _run(test, 4, red=True),
        output=(
            "_______________________ test_feature _______________________\n"
            "E       KeyError: 'seat'\n"
            "tests/test_feature.py:4: KeyError\n"
            "FAILED tests/test_feature.py::test_feature - KeyError: 'seat'\n"
        ),
    )
    return test, TranscriptEvidence(
        edits=(
            before_test,
            _edit(test.path, 1, source),
            repeated_write,
            cleanup,
            _edit(module_path, 5),
        ),
        validation_runs=(red, _run(test, 6, red=False)),
    )


@pytest.mark.parametrize("module_path", ["src/feature.py", "src/feature/__init__.py"])
def test_unchanged_move_red(module_path: str) -> None:
    test, evidence = _move_cycle(module_path)

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (f"pytest {test.reference} -q",)


@pytest.mark.parametrize(
    "changed",
    [
        "body",
        "unconfirmed",
        "late_baseline",
        "dependency",
        "rebound",
        "import-rebound",
        "class-rebound",
        "test-rebound",
        "other-invoked-api",
        "reconstructed",
    ],
)
def test_move_needs_proven_unchanged_api(changed: str) -> None:
    test, evidence = _move_cycle()
    edits = list(evidence.edits)
    if changed == "body":
        edits[2] = replace(edits[2], source_after="def payload():\n    return {'other': 1}\n")
    elif changed == "unconfirmed":
        edits[2] = replace(edits[2], source_confirmed=False, source_confirmed_at=None)
    elif changed == "late_baseline":
        edits = edits[1:]
    elif changed == "dependency":
        dependent = "from dependency import value\ndef payload():\n    return value()\n"
        edits[0] = replace(edits[0], source_after=dependent)
        edits[2] = replace(edits[2], source_after=dependent)
        edits[3] = replace(edits[3], path="src/dependency.py", source_fragment=None)
    elif changed in {"rebound", "import-rebound", "class-rebound"}:
        binding = {
            "rebound": "payload = lambda: {}\n",
            "import-rebound": "from dependency import payload\n",
            "class-rebound": "class payload:\n    pass\n",
        }[changed]
        rebound = (edits[0].source_after or "") + binding
        edits[0] = replace(edits[0], source_after=rebound)
        edits[2] = replace(edits[2], source_after=rebound)
    elif changed == "test-rebound":
        original = edits[1].source_after or ""
        edits[1] = replace(edits[1], source_after=original + "payload = lambda: {}\n")
    elif changed == "other-invoked-api":
        original = edits[1].source_after or ""
        edits[1] = replace(
            edits[1],
            source_after="from other import implemented\n"
            + original.replace("    assert payload()", "    implemented()\n    assert payload()"),
        )
        edits[3] = replace(
            edits[3], path="src/other.py", source_after="def implemented():\n    return 42\n"
        )
    else:
        edits = [replace(edit, order=edit.order * 10) for edit in edits]
        evidence = replace(
            evidence,
            validation_runs=tuple(
                replace(run, order=run.order * 10) for run in evidence.validation_runs
            ),
        )
        implemented = replace(
            _edit("src/feature.py", 2, "def payload():\n    return {'seat': 'implemented'}\n"),
            order=15,
            timestamp=START + timedelta(seconds=15),
            source_confirmed_at=START + timedelta(seconds=16),
        )
        edits.insert(2, implemented)

    result = evaluate_tdd_evidence((test,), replace(evidence, edits=tuple(edits)))

    assert not result.passed
    assert not result.red_runs
