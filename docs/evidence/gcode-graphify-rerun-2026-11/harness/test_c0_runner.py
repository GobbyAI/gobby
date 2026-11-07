"""C0 runs the matrix's commands against one frozen manifest and fails closed on each pin."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from c0_poller import IndexPoller
from c0_runner import (
    COLD_INDEX_NAME,
    GRAPHIFY_BINARY,
    GRAPHIFY_MARKER_PATTERN,
    GRAPHIFY_PIN,
    PROBE_SYMBOLS,
    CommandResult,
    Runner,
    build_command_manifest,
    c7_poller_marker_ok,
    decide_graphify_concurrency,
    evaluate_c0,
    freeze_command_manifest,
    probe_graphify_concurrency,
    require_probe_spread,
    require_probe_symbols,
    run_c0,
    run_process,
    september_max_workers,
)
from runtime_boundary import EvidenceError

SYMBOLS = PROBE_SYMBOLS
ARCHIVED_GCODE = "1.9.104"
SYMBOL_FILES = {
    "load_policy": "platform/settings.py",
    "repository_root": "platform/settings.py",
    "SettingsError": "platform/settings.py",
    "plan_product": "replenishment/planner.py",
    "MCPError": "platform/mcp.py",
}
SEPTEMBER = {
    "value": 1,
    "source": "september",
    "flag": "--max-workers",
    "evidence": [
        "/runtime/logs/c0/attempt-1/probe/graphify-extract-help.stdout:3",
        "/september/results/index-comparison/run-records.jsonl:3",
    ],
}
UNSUPPORTED = {
    "value": None,
    "source": "unsupported",
    "flag": None,
    "evidence": ["/runtime/logs/c0/attempt-1/probe/graphify-extract-help.stdout"],
}
GRAPHIFY_HELP = "\n".join(
    f"  {name}"
    for name in (
        "extract",
        "update",
        "path",
        "explain",
        "query",
        "affected",
        "cluster-only",
        "label",
        "check-update",
        "tree",
        "benchmark",
        "export",
    )
)
EXPORT_HELP = "  callflow-html  Export a call-flow HTML page\n"
EXTRACT_HELP_WITH_FLAG = (
    "usage: graphify extract PATH\n"
    "  --code-only\n"
    "  --max-workers N  AST extraction subprocess count (default: cpu_count)\n"
)
EXTRACT_HELP_WITHOUT_FLAG = "usage: graphify extract PATH\n  --code-only\n"
SEPTEMBER_EXTRACT_OUTPUT = (
    "[graphify extract] AST extraction on 119 code files...\n"
    "  AST extraction: 100/119 uncached files (84%)\n"
    "  AST extraction: 119/119 uncached files (100%)\n"
)
GOOD_STATUS = {
    "id": "case-id",
    "root_path": "/runtime/corpora/gcode/C0",
    "code_index": {"healthy": True},
}
CHECK_RULINGS: dict[str, Any] = {
    "project_id": "case-id",
    "c7_poller_rows": 4,
    "c7_clean_count": 9,
    "graphify_marker_pattern": GRAPHIFY_MARKER_PATTERN,
}


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def _manifest(concurrency: dict[str, Any] = SEPTEMBER) -> dict[str, Any]:
    return build_command_manifest(
        Path("/runtime"),
        probe_symbols=SYMBOLS,
        graphify_concurrency=concurrency,
        gcode_version=ARCHIVED_GCODE,
    )


def _stage_archive(root: Path, version: str = ARCHIVED_GCODE) -> None:
    crate = root / "sources/gobby/crates/gcode"
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(f'[package]\nname = "gcode"\nversion = "{version}"\n')


@pytest.fixture(autouse=True)
def _archived_gcode(tmp_path: Path) -> None:
    _stage_archive(tmp_path)


def _extract_argv(manifest: dict[str, Any]) -> list[str]:
    return next(item["argv"] for item in manifest["commands"] if item["name"] == "graphify-extract")


def _stage_corpus(root: Path, symbols: tuple[str, ...] = SYMBOLS) -> None:
    source = root / "corpora/graphify/C0/src"
    for symbol in symbols:
        path = source / SYMBOL_FILES[symbol]
        path.parent.mkdir(parents=True, exist_ok=True)
        definition = (
            f"class {symbol}(RuntimeError):\n    pass\n"
            if symbol[0].isupper()
            else f"def {symbol}():\n    return None\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(definition)


def _stage_september(root: Path) -> Path:
    results = root / "results/index-comparison"
    results.mkdir(parents=True)
    records = [
        {"command_argv": ["graphify", "check-update"]},
        {"command_argv": ["graphify", "update"]},
        {
            "command_argv": ["graphify", "extract", "--code-only", "--max-workers", "1"],
            "redacted_environment_diff": {"GRAPHIFY_NATIVE_CONCURRENCY": "1"},
        },
    ]
    (results / "run-records.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records))
    return root


def _passing_outputs(manifest: dict[str, Any]) -> dict[str, CommandResult]:
    outputs = {
        "graphify-version": _ok(f"graphify {GRAPHIFY_PIN}\n"),
        "graphify-help": _ok(GRAPHIFY_HELP),
        "graphify-export-help": _ok(EXPORT_HELP),
        "graphify-extract": _ok(SEPTEMBER_EXTRACT_OUTPUT),
        COLD_INDEX_NAME: _ok(),
        "gcode-version": _ok(f"gcode {ARCHIVED_GCODE}\n"),
        "gcode-contract": _ok(json.dumps({"tool": "gcode", "contract_version": 12})),
        "gcode-schema-identity": _ok(json.dumps({"assets_root_hash": "abc"})),
        "gcode-embeddings-doctor": _ok(json.dumps({"agrees": True})),
        "gcode-status": _ok(json.dumps(GOOD_STATUS)),
    }
    for command in manifest["commands"]:
        if command["name"].startswith("gcode-callers-"):
            outputs[command["name"]] = _ok(json.dumps({"total": 1, "results": []}))
    return outputs


def _checks(outputs: dict[str, CommandResult], **rulings: Any) -> dict[str, bool]:
    arguments = {**CHECK_RULINGS, **rulings}
    return {
        check["check"]: check["passed"] for check in evaluate_c0(outputs, _manifest(), **arguments)
    }


def _runner_for(
    root: Path, outputs: dict[str, CommandResult], concurrency: dict[str, Any] = SEPTEMBER
) -> Runner:
    manifest = build_command_manifest(
        root, probe_symbols=SYMBOLS, graphify_concurrency=concurrency, gcode_version=ARCHIVED_GCODE
    )
    names = {tuple(command["argv"]): command["name"] for command in manifest["commands"]}

    def fake_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        return outputs[names[tuple(argv)]]

    return fake_runner


def _probe_runner(root: Path, extract_help: str) -> Runner:
    graphify = str(root / GRAPHIFY_BINARY)
    answers = {
        (graphify, "--help"): _ok(GRAPHIFY_HELP),
        (graphify, "extract", "--help"): _ok(extract_help),
    }

    def fake_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        return answers[tuple(argv)]

    return fake_runner


def _observer(tmp_path: Path, count: int) -> IndexPoller:
    return IndexPoller(
        count=lambda: count,
        clock=time.monotonic,
        clean=9,
        log=tmp_path / "poller.jsonl",
        interval=0.001,
    )


def _run_kwargs(tmp_path: Path, count: int = 4) -> dict[str, Any]:
    return {
        "observer": _observer(tmp_path, count),
        "graphify_marker_pattern": GRAPHIFY_MARKER_PATTERN,
        "project_id": "case-id",
    }


def test_manifest_lists_every_matrix_command_with_pins_and_the_concurrency_receipt() -> None:
    manifest = _manifest()
    commands = {command["name"]: command for command in manifest["commands"]}

    assert commands["graphify-version"]["argv"] == [
        "/runtime/tools/graphify/venv/bin/graphify",
        "--version",
    ]
    assert _extract_argv(manifest) == [
        "/runtime/tools/graphify/venv/bin/graphify",
        "extract",
        "/runtime/corpora/graphify/C0",
        "--code-only",
        "--no-cluster",
        "--out",
        "/runtime/c0/graphify-state",
        "--max-workers",
        "1",
    ]
    assert commands["gcode-contract"]["argv"] == [
        "/runtime/tools/gcode/bin/gcode",
        "--format",
        "json",
        "contract",
    ]
    assert commands["gcode-schema-identity"]["argv"][-2:] == ["schema-identity", "--json"]
    assert commands["gcode-embeddings-doctor"]["argv"][-2:] == ["embeddings", "doctor"]
    assert commands["gcode-status"]["argv"][-1] == "status"
    assert "graphify-export-help" in commands
    assert [name for name in commands if name.startswith("gcode-callers-")] == [
        f"gcode-callers-{index}" for index in range(1, len(SYMBOLS) + 1)
    ]
    assert manifest["pins"] == {
        "graphify": GRAPHIFY_PIN,
        "gcode": ARCHIVED_GCODE,
        "gcode_contract": 12,
    }
    assert manifest["graphify_concurrency"] == SEPTEMBER


def test_the_cold_index_is_the_first_gcode_command_and_runs_the_september_index_argv() -> None:
    gcode_commands = [item for item in _manifest()["commands"] if item["name"].startswith("gcode-")]

    assert gcode_commands[0]["name"] == COLD_INDEX_NAME
    assert gcode_commands[0]["argv"] == [
        "/runtime/tools/gcode/bin/gcode",
        "--project",
        "/runtime/corpora/gcode/C0",
        "--format",
        "json",
        "index",
        "/runtime/corpora/gcode/C0",
        "--full",
        "--sync-projections",
    ]


def test_the_extract_takes_the_frozen_max_workers_only_when_the_receipt_records_it() -> None:
    assert _extract_argv(_manifest())[-2:] == ["--max-workers", "1"]
    assert "--max-workers" not in _extract_argv(_manifest(UNSUPPORTED))


@pytest.mark.parametrize(
    "receipt",
    [
        {**SEPTEMBER, "value": 0},
        {**SEPTEMBER, "value": True},
        {**SEPTEMBER, "value": 1.5},
        {**SEPTEMBER, "source": "unsupported"},
        {**SEPTEMBER, "flag": "--workers"},
        {**SEPTEMBER, "evidence": ["only one"]},
        {**UNSUPPORTED, "source": "september"},
    ],
)
def test_manifest_refuses_a_concurrency_receipt_that_is_not_the_recorded_shape(
    receipt: dict[str, Any],
) -> None:
    with pytest.raises(EvidenceError, match="concurrency receipt"):
        _manifest(receipt)


def test_the_probe_pins_september_only_when_this_releases_extract_help_lists_the_flag() -> None:
    help_path = Path("/runtime/logs/c0/attempt-1/probe/graphify-extract-help.stdout")

    decided = decide_graphify_concurrency(
        _ok(EXTRACT_HELP_WITH_FLAG), help_path, 1, "/september/run-records.jsonl:3"
    )

    assert decided == {
        "value": 1,
        "source": "september",
        "flag": "--max-workers",
        "evidence": [f"{help_path}:3", "/september/run-records.jsonl:3"],
    }


def test_the_probe_falls_back_to_default_workers_when_the_flag_is_absent() -> None:
    help_path = Path("/runtime/logs/c0/attempt-1/probe/graphify-extract-help.stdout")

    decided = decide_graphify_concurrency(
        _ok(EXTRACT_HELP_WITHOUT_FLAG), help_path, 1, "/september/run-records.jsonl:3"
    )

    assert decided == {
        "value": None,
        "source": "unsupported",
        "flag": None,
        "evidence": [str(help_path)],
    }


def test_a_failed_extract_help_probe_stops_the_concurrency_decision() -> None:
    failed = CommandResult(stdout="", stderr="boom", returncode=2)

    with pytest.raises(EvidenceError, match="probe failed"):
        decide_graphify_concurrency(failed, Path("/runtime/help"), 1, "/september/records:3")


def test_september_concurrency_is_the_c0_extract_records_value_and_its_line(tmp_path: Path) -> None:
    september = _stage_september(tmp_path / "september")

    value, evidence = september_max_workers(september)

    assert value == 1
    assert evidence == f"{september / 'results/index-comparison/run-records.jsonl'}:3"


def test_september_concurrency_is_refused_when_the_record_does_not_carry_it(
    tmp_path: Path,
) -> None:
    records = tmp_path / "september/results/index-comparison"
    records.mkdir(parents=True)
    (records / "run-records.jsonl").write_text(
        '{"x": 1}\n{"x": 2}\n{"redacted_environment_diff": {}}\n'
    )

    with pytest.raises(EvidenceError, match="records no positive GRAPHIFY_NATIVE_CONCURRENCY"):
        september_max_workers(tmp_path / "september")


def test_the_probe_saves_both_help_captures_and_decides_from_the_extract_capture(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    september = _stage_september(tmp_path / "september")

    receipt = probe_graphify_concurrency(
        root,
        "attempt-1",
        _probe_runner(root, EXTRACT_HELP_WITH_FLAG),
        september_root=september,
        env={},
    )

    probe_dir = root / "logs/c0/attempt-1/probe"
    assert (probe_dir / "graphify-help.stdout").read_text() == GRAPHIFY_HELP
    assert (probe_dir / "graphify-extract-help.stdout").read_text() == EXTRACT_HELP_WITH_FLAG
    assert receipt["source"] == "september"
    assert receipt["evidence"][0] == f"{probe_dir / 'graphify-extract-help.stdout'}:3"


def test_the_five_ruled_symbols_are_defined_and_span_three_files(tmp_path: Path) -> None:
    _stage_corpus(tmp_path)
    corpus = tmp_path / "corpora/graphify/C0"

    require_probe_symbols(corpus, SYMBOLS)

    assert require_probe_spread(corpus, SYMBOLS) == {
        "src/platform/settings.py",
        "src/replenishment/planner.py",
        "src/platform/mcp.py",
    }


def test_probe_symbols_defined_in_fewer_than_three_files_are_refused(tmp_path: Path) -> None:
    source = tmp_path / "corpora/graphify/C0/src"
    source.mkdir(parents=True)
    (source / "settings.py").write_text(
        "".join(f"def {symbol}():\n    return None\n" for symbol in SYMBOLS)
    )

    with pytest.raises(EvidenceError, match="span 1 files"):
        require_probe_spread(tmp_path / "corpora/graphify/C0", SYMBOLS)


def test_freeze_writes_the_manifest_once_and_refuses_a_changed_command_set(tmp_path: Path) -> None:
    first = freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)

    assert (
        freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
        == first
    )
    with pytest.raises(EvidenceError, match="command manifest changed"):
        freeze_command_manifest(tmp_path, probe_symbols=("other",), graphify_concurrency=SEPTEMBER)
    with pytest.raises(EvidenceError, match="command manifest changed"):
        freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=UNSUPPORTED)


def test_run_saves_each_output_and_records_the_concurrency_and_the_c7_observation(
    tmp_path: Path,
) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    outputs = _passing_outputs(_manifest())

    receipt = run_c0(
        tmp_path,
        "attempt-1",
        _runner_for(tmp_path, outputs),
        ambient={"PATH": "/usr/bin", "OPENAI_API_KEY": "do-not-record"},
        **_run_kwargs(tmp_path),
    )

    log_dir = tmp_path / "logs/c0/attempt-1"
    assert (log_dir / "graphify-version.stdout").read_text() == f"graphify {GRAPHIFY_PIN}\n"
    assert (log_dir / "graphify-version.stderr").read_text() == ""
    contract = next(item for item in receipt["results"] if item["name"] == "gcode-contract")
    assert contract["exit_code"] == 0
    assert "OPENAI_API_KEY" in contract["env_diff"]["removed"]
    assert "do-not-record" not in json.dumps(receipt)
    assert (tmp_path / "receipts/c0-attempt-1.json").exists()
    assert receipt["graphify_concurrency"] == SEPTEMBER
    assert receipt["c7"] == {"first_qualifying": 4, "clean": 9}


def test_run_writes_the_probe_symbols_before_any_command_runs(tmp_path: Path) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    answer = _runner_for(tmp_path, _passing_outputs(_manifest()))
    symbols_receipt = tmp_path / "receipts/c0-symbols-attempt-3.json"
    seen_before_first_command: list[bool] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        seen_before_first_command.append(symbols_receipt.exists())
        return answer(argv, cwd, env)

    run_c0(tmp_path, "attempt-3", runner, ambient={}, **_run_kwargs(tmp_path))

    assert seen_before_first_command[0] is True
    assert json.loads(symbols_receipt.read_text())["probe_symbols"] == list(SYMBOLS)


def test_run_refuses_a_probe_symbol_missing_from_the_pinned_corpus_before_any_command(
    tmp_path: Path,
) -> None:
    _stage_corpus(tmp_path, symbols=SYMBOLS[:-1])
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    ran: list[str] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        ran.append(argv[0])
        return _ok()

    with pytest.raises(EvidenceError, match="missing from pinned corpus"):
        run_c0(tmp_path, "attempt-4", runner, ambient={}, **_run_kwargs(tmp_path))

    assert ran == []
    assert not (tmp_path / "receipts/c0-symbols-attempt-4.json").exists()


def test_require_probe_symbols_counts_only_a_definition_not_a_mention(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "caller.py").write_text("value = load_policy()\n")

    with pytest.raises(EvidenceError, match="missing from pinned corpus"):
        require_probe_symbols(tmp_path, ("load_policy",))


def test_run_records_failure_and_does_not_pass_when_a_check_fails(tmp_path: Path) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    outputs = _passing_outputs(_manifest())
    outputs["gcode-embeddings-doctor"] = CommandResult(stdout="{}", stderr="drift", returncode=11)

    receipt = run_c0(
        tmp_path, "attempt-2", _runner_for(tmp_path, outputs), ambient={}, **_run_kwargs(tmp_path)
    )

    assert receipt["passed"] is False
    assert (tmp_path / "receipts/c0-attempt-2.json").exists()


@pytest.mark.parametrize("count", [0, 9])
def test_run_fails_when_the_index_is_never_observed_partway(tmp_path: Path, count: int) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    outputs = _passing_outputs(_manifest())

    receipt = run_c0(
        tmp_path,
        "attempt-5",
        _runner_for(tmp_path, outputs),
        ambient={},
        **_run_kwargs(tmp_path, count=count),
    )

    checks = {item["check"]: item["passed"] for item in receipt["checks"]}
    assert checks["c7-poller-marker"] is False
    assert receipt["passed"] is False


def test_a_failed_cold_index_stops_c0_with_its_exact_stderr_and_no_passing_receipt(
    tmp_path: Path,
) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    names = [
        command["name"]
        for command in json.loads((tmp_path / "c0/command-manifest.json").read_text())["commands"]
    ]
    outputs = _passing_outputs(_manifest())
    stderr = "malformed grant: grant project does not match local project\n"
    outputs[COLD_INDEX_NAME] = CommandResult(stdout="", stderr=stderr, returncode=1)
    answer = _runner_for(tmp_path, outputs)
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        calls.append(argv)
        return answer(argv, cwd, env)

    with pytest.raises(EvidenceError, match="malformed grant: grant project does not match"):
        run_c0(tmp_path, "attempt-6", runner, ambient={}, **_run_kwargs(tmp_path))

    assert len(calls) == names.index(COLD_INDEX_NAME) + 1
    assert (tmp_path / "logs/c0/attempt-6/gcode-index-cold.stderr").read_text() == stderr
    assert not (tmp_path / "receipts/c0-attempt-6.json").exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_a_cold_index_whose_poller_also_fails_keeps_its_logs_and_raises_the_poller_failure(
    tmp_path: Path, returncode: int
) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    outputs = _passing_outputs(_manifest())
    cold = CommandResult(
        stdout="cold index stdout\n", stderr="cold index stderr\n", returncode=returncode
    )
    outputs[COLD_INDEX_NAME] = cold

    def failing_count() -> int:
        raise psycopg.OperationalError("owned database unreachable")

    kwargs = _run_kwargs(tmp_path)
    kwargs["observer"] = IndexPoller(
        count=failing_count,
        clock=time.monotonic,
        clean=9,
        log=tmp_path / "poller.jsonl",
        interval=0.001,
    )

    with pytest.raises(
        EvidenceError, match="index poller failed: OperationalError: owned database unreachable"
    ):
        run_c0(tmp_path, "attempt-7", _runner_for(tmp_path, outputs), ambient={}, **kwargs)

    log_dir = tmp_path / "logs/c0/attempt-7"
    assert (log_dir / "gcode-index-cold.stdout").read_text() == cold.stdout
    assert (log_dir / "gcode-index-cold.stderr").read_text() == cold.stderr
    assert not (tmp_path / "receipts/c0-attempt-7.json").exists()


def test_run_process_returns_the_stdout_and_exit_code_of_the_frozen_argv(tmp_path: Path) -> None:
    result = run_process(
        [sys.executable, "-c", "import sys; print('ok'); sys.exit(3)"],
        tmp_path,
        {"PATH": "/usr/bin"},
    )

    assert result == CommandResult(stdout="ok\n", stderr="", returncode=3)


def test_graphify_pin_requires_the_exact_version_token() -> None:
    assert _checks(_passing_outputs(_manifest()))["graphify-pin"] is True
    for wrong in ("graphify 0.9.1030", "graphify 0.9.55", "graphify 1.0.0"):
        outputs = {**_passing_outputs(_manifest()), "graphify-version": _ok(wrong)}
        assert _checks(outputs)["graphify-pin"] is False


def test_gcode_pin_requires_the_exact_version_line() -> None:
    assert _checks(_passing_outputs(_manifest()))["gcode-pin"] is True
    outputs = {**_passing_outputs(_manifest()), "gcode-version": _ok("gcode 1.9.1040\n")}
    assert _checks(outputs)["gcode-pin"] is False


def test_gcode_contract_requires_the_gcode_tool_at_contract_twelve() -> None:
    assert _checks(_passing_outputs(_manifest()))["gcode-contract"] is True
    for payload in (
        {"tool": "gcode", "contract_version": 8},
        {"tool": "other", "contract_version": 12},
    ):
        outputs = {**_passing_outputs(_manifest()), "gcode-contract": _ok(json.dumps(payload))}
        assert _checks(outputs)["gcode-contract"] is False


@pytest.mark.parametrize(
    ("exit_code", "payload", "expected"),
    [
        (0, {"agrees": True}, True),
        (0, {"agrees": None}, False),
        (0, {}, False),
        (0, {"agrees": False}, False),
        (10, {"agrees": True}, False),
        (11, {"agrees": True}, False),
        (20, {"agrees": True}, False),
    ],
)
def test_embedding_preflight_passes_only_on_exit_zero_and_agreement(
    exit_code: int, payload: dict[str, object], expected: bool
) -> None:
    outputs = _passing_outputs(_manifest())
    outputs["gcode-embeddings-doctor"] = CommandResult(
        stdout=json.dumps(payload), stderr="", returncode=exit_code
    )

    assert _checks(outputs)["embedding-preflight"] is expected


def test_status_requires_an_indexed_and_healthy_code_index() -> None:
    assert _checks(_passing_outputs(_manifest()))["gcode-status-indexed"] is True
    unhealthy = {**GOOD_STATUS, "code_index": {"healthy": False}}
    for payload in (unhealthy, {"status": "unindexed"}):
        outputs = {**_passing_outputs(_manifest()), "gcode-status": _ok(json.dumps(payload))}
        assert _checks(outputs)["gcode-status-indexed"] is False


def test_graphify_inventory_requires_every_matrix_command_and_the_callflow_export() -> None:
    assert _checks(_passing_outputs(_manifest()))["graphify-inventory"] is True
    missing_tree = _passing_outputs(_manifest())
    missing_tree["graphify-help"] = _ok(GRAPHIFY_HELP.replace("  tree", "  treeless"))
    assert _checks(missing_tree)["graphify-inventory"] is False
    no_callflow = {**_passing_outputs(_manifest()), "graphify-export-help": _ok("  json\n")}
    assert _checks(no_callflow)["graphify-inventory"] is False


def test_gcode_schema_identity_is_recorded_but_is_not_a_c0_gate() -> None:
    outputs = _passing_outputs(_manifest())
    outputs["gcode-schema-identity"] = _ok(json.dumps({"anything": "else"}))

    assert "gcode-schema-identity" not in _checks(outputs)


def test_the_gcode_pin_is_the_archived_crate_version_frozen_into_the_manifest(
    tmp_path: Path,
) -> None:
    _stage_archive(tmp_path, "1.9.105")

    manifest = json.loads(
        freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=SEPTEMBER)
    )

    assert manifest["pins"]["gcode"] == "1.9.105"


def test_the_gcode_pin_check_compares_with_the_frozen_manifest_pin() -> None:
    manifest = _manifest()
    manifest["pins"]["gcode"] = "1.9.105"

    checks = {
        check["check"]: check["passed"]
        for check in evaluate_c0(_passing_outputs(manifest), manifest, **CHECK_RULINGS)
    }

    assert checks["gcode-pin"] is False


def test_the_gcode_status_id_must_be_the_corpus_project_id() -> None:
    passing = _passing_outputs(_manifest())
    assert _checks(passing).get("gcode-status-project-id") is True

    other_id = {**GOOD_STATUS, "id": "other-id"}
    no_id = {key: value for key, value in GOOD_STATUS.items() if key != "id"}
    for status in (other_id, no_id):
        outputs = {**passing, "gcode-status": _ok(json.dumps(status))}
        assert _checks(outputs).get("gcode-status-project-id") is False


def test_probe_set_fails_closed_without_the_ruled_symbols() -> None:
    manifest = build_command_manifest(
        Path("/runtime"),
        probe_symbols=(),
        graphify_concurrency=SEPTEMBER,
        gcode_version=ARCHIVED_GCODE,
    )
    checks = {
        check["check"]: check["passed"]
        for check in evaluate_c0(_passing_outputs(manifest), manifest, **CHECK_RULINGS)
    }

    assert checks["probe-set"] is False


def test_probe_set_fails_when_a_callers_probe_produced_no_output() -> None:
    outputs = _passing_outputs(_manifest())
    del outputs["gcode-callers-2"]

    assert _checks(outputs)["probe-set"] is False


@pytest.mark.parametrize(
    ("receipt", "accepted"),
    [
        (SEPTEMBER, True),
        (UNSUPPORTED, True),
        ({**UNSUPPORTED, "value": 1}, False),
        ({**SEPTEMBER, "source": "unsupported"}, False),
    ],
)
def test_the_concurrency_check_accepts_only_the_recorded_receipt_shapes(
    receipt: dict[str, Any], accepted: bool
) -> None:
    manifest = _manifest()
    manifest["graphify_concurrency"] = receipt
    checks = {
        check["check"]: check["passed"]
        for check in evaluate_c0(_passing_outputs(manifest), manifest, **CHECK_RULINGS)
    }

    assert checks["graphify-concurrency"] is accepted


def test_graphify_marker_stays_pending_until_its_pattern_is_ruled() -> None:
    assert (
        _checks(_passing_outputs(_manifest()), graphify_marker_pattern=None)["graphify-marker"]
        is False
    )
    assert _checks(_passing_outputs(_manifest()))["graphify-marker"] is True


def test_graphify_marker_is_the_september_pattern_and_passes_only_when_observed() -> None:
    assert GRAPHIFY_MARKER_PATTERN == r"AST extraction: 100/119 uncached files \(84%\)"
    outputs = _passing_outputs(_manifest())
    assert _checks(outputs)["graphify-marker"] is True
    outputs["graphify-extract"] = _ok("  AST extraction: 119/119 uncached files (100%)\n")

    assert _checks(outputs)["graphify-marker"] is False


@pytest.mark.parametrize(
    ("rows", "clean", "expected"),
    [(None, 9, False), (0, 9, False), (9, 9, False), (1, 9, True), (8, 9, True)],
)
def test_c7_poller_marker_requires_rows_strictly_between_zero_and_the_clean_count(
    rows: int | None, clean: int, expected: bool
) -> None:
    assert c7_poller_marker_ok(rows, clean) is expected
