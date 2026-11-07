"""C0 runs the matrix's commands against one frozen manifest and fails closed on each pin."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from c0_runner import (
    GCODE_PIN,
    GRAPHIFY_MARKER_PATTERN,
    GRAPHIFY_PIN,
    PROBE_SYMBOLS,
    CommandResult,
    Runner,
    build_command_manifest,
    c7_poller_marker_ok,
    evaluate_c0,
    freeze_command_manifest,
    require_probe_symbols,
    run_c0,
    run_process,
    select_native_concurrency,
)
from runtime_boundary import EvidenceError

SYMBOLS = PROBE_SYMBOLS
CONCURRENCY = 2
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
RULINGS: dict[str, Any] = {
    "expected_schema_identity": {"assets_root_hash": "abc"},
    "c7_poller_rows": 3,
    "c7_clean_count": 9,
    "graphify_marker_pattern": GRAPHIFY_MARKER_PATTERN,
}


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def _manifest() -> dict[str, Any]:
    return build_command_manifest(
        Path("/runtime"), probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY
    )


def _stage_corpus(root: Path, symbols: tuple[str, ...] = SYMBOLS) -> None:
    source = root / "corpora/graphify/C0/src"
    source.mkdir(parents=True)
    (source / "settings.py").write_text(
        "".join(f"def {symbol}():\n    return None\n" for symbol in symbols)
    )


def _passing_outputs(manifest: dict[str, Any]) -> dict[str, CommandResult]:
    outputs = {
        "graphify-version": _ok(f"graphify {GRAPHIFY_PIN}\n"),
        "graphify-help": _ok(GRAPHIFY_HELP),
        "graphify-export-help": _ok(EXPORT_HELP),
        "graphify-extract": _ok(SEPTEMBER_EXTRACT_OUTPUT),
        "gcode-version": _ok(f"gcode {GCODE_PIN}\n"),
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
    arguments = {**RULINGS, **rulings}
    return {
        check["check"]: check["passed"] for check in evaluate_c0(outputs, _manifest(), **arguments)
    }


def _runner_for(
    root: Path, outputs: dict[str, CommandResult], seen: list[tuple[str, dict[str, str]]]
) -> Runner:
    manifest = build_command_manifest(root, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    names = {tuple(command["argv"]): command["name"] for command in manifest["commands"]}

    def fake_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        name = names[tuple(argv)]
        seen.append((name, env))
        return outputs[name]

    return fake_runner


def test_manifest_lists_every_matrix_command_with_pins_and_native_concurrency() -> None:
    manifest = _manifest()
    commands = {command["name"]: command for command in manifest["commands"]}

    assert commands["graphify-version"]["argv"] == [
        "/runtime/tools/graphify/venv/bin/graphify",
        "--version",
    ]
    assert commands["graphify-extract"]["argv"] == [
        "/runtime/tools/graphify/venv/bin/graphify",
        "extract",
        "/runtime/corpora/graphify/C0",
        "--code-only",
        "--no-cluster",
        "--out",
        "/runtime/c0/graphify-state",
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
    assert commands["graphify-extract"]["env"] == {"GRAPHIFY_NATIVE_CONCURRENCY": str(CONCURRENCY)}
    assert commands["gcode-version"]["env"] == {}
    assert manifest["pins"] == {"graphify": GRAPHIFY_PIN, "gcode": GCODE_PIN, "gcode_contract": 12}


def test_manifest_takes_the_concurrency_from_the_probe_and_rejects_a_non_positive_value() -> None:
    for value in (0, -1, True, 1.5):
        bad: Any = value
        with pytest.raises(EvidenceError, match="positive integer"):
            build_command_manifest(
                Path("/runtime"), probe_symbols=SYMBOLS, graphify_concurrency=bad
            )


def test_select_native_concurrency_takes_the_lowest_positive_observed_limit() -> None:
    assert select_native_concurrency([4, 2, 8]) == 2


@pytest.mark.parametrize("limits", [[], [0], [3, -1], [True]])
def test_select_native_concurrency_refuses_an_empty_or_non_positive_probe(
    limits: list[Any],
) -> None:
    with pytest.raises(EvidenceError):
        select_native_concurrency(limits)


def test_freeze_writes_the_manifest_once_and_refuses_a_changed_command_set(tmp_path: Path) -> None:
    first = freeze_command_manifest(
        tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY
    )

    assert (
        freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
        == first
    )
    with pytest.raises(EvidenceError, match="command manifest changed"):
        freeze_command_manifest(
            tmp_path, probe_symbols=("other",), graphify_concurrency=CONCURRENCY
        )
    with pytest.raises(EvidenceError, match="command manifest changed"):
        freeze_command_manifest(
            tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY + 1
        )


def test_run_saves_each_output_and_records_environment_names_without_values(tmp_path: Path) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    outputs = _passing_outputs(
        build_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    )
    seen: list[tuple[str, dict[str, str]]] = []

    receipt = run_c0(
        tmp_path,
        "attempt-1",
        _runner_for(tmp_path, outputs, seen),
        ambient={"PATH": "/usr/bin", "OPENAI_API_KEY": "do-not-record"},
        **RULINGS,
    )

    log_dir = tmp_path / "logs/c0/attempt-1"
    assert (log_dir / "graphify-version.stdout").read_text() == f"graphify {GRAPHIFY_PIN}\n"
    assert (log_dir / "graphify-version.stderr").read_text() == ""
    contract = next(item for item in receipt["results"] if item["name"] == "gcode-contract")
    assert contract["exit_code"] == 0
    assert "OPENAI_API_KEY" in contract["env_diff"]["removed"]
    assert "do-not-record" not in json.dumps(receipt)
    assert (tmp_path / "receipts/c0-attempt-1.json").exists()
    envs = dict(seen)
    assert envs["graphify-version"]["GRAPHIFY_NATIVE_CONCURRENCY"] == str(CONCURRENCY)
    assert "GRAPHIFY_NATIVE_CONCURRENCY" not in envs["gcode-version"]


def test_run_writes_the_probe_symbols_before_any_command_runs(tmp_path: Path) -> None:
    _stage_corpus(tmp_path)
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    outputs = _passing_outputs(
        build_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    )
    answer = _runner_for(tmp_path, outputs, [])
    symbols_receipt = tmp_path / "receipts/c0-symbols-attempt-3.json"
    seen_before_first_command: list[bool] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        seen_before_first_command.append(symbols_receipt.exists())
        return answer(argv, cwd, env)

    run_c0(tmp_path, "attempt-3", runner, ambient={}, **RULINGS)

    assert seen_before_first_command[0] is True
    assert json.loads(symbols_receipt.read_text())["probe_symbols"] == list(SYMBOLS)


def test_run_refuses_a_probe_symbol_missing_from_the_pinned_corpus_before_any_command(
    tmp_path: Path,
) -> None:
    _stage_corpus(tmp_path, symbols=SYMBOLS[:-1])
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    ran: list[str] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        ran.append(argv[0])
        return _ok()

    with pytest.raises(EvidenceError, match="missing from pinned corpus"):
        run_c0(tmp_path, "attempt-4", runner, ambient={}, **RULINGS)

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
    freeze_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    outputs = _passing_outputs(
        build_command_manifest(tmp_path, probe_symbols=SYMBOLS, graphify_concurrency=CONCURRENCY)
    )
    outputs["gcode-embeddings-doctor"] = CommandResult(stdout="{}", stderr="drift", returncode=11)

    receipt = run_c0(
        tmp_path, "attempt-2", _runner_for(tmp_path, outputs, []), ambient={}, **RULINGS
    )

    assert receipt["passed"] is False
    assert (tmp_path / "receipts/c0-attempt-2.json").exists()


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


def test_schema_identity_stays_pending_until_its_expected_value_is_ruled() -> None:
    outputs = _passing_outputs(_manifest())

    assert _checks(outputs, expected_schema_identity=None)["gcode-schema-identity"] is False
    assert _checks(outputs)["gcode-schema-identity"] is True


def test_probe_set_fails_closed_without_the_ruled_symbols() -> None:
    manifest = build_command_manifest(
        Path("/runtime"), probe_symbols=(), graphify_concurrency=CONCURRENCY
    )
    checks = {
        check["check"]: check["passed"]
        for check in evaluate_c0(_passing_outputs(manifest), manifest, **RULINGS)
    }

    assert checks["probe-set"] is False


def test_probe_set_fails_when_a_callers_probe_produced_no_output() -> None:
    outputs = _passing_outputs(_manifest())
    del outputs["gcode-callers-2"]

    assert _checks(outputs)["probe-set"] is False


def test_graphify_marker_stays_pending_until_its_pattern_is_ruled() -> None:
    assert (
        _checks(_passing_outputs(_manifest()), graphify_marker_pattern=None)["graphify-marker"]
        is False
    )
    assert _checks(_passing_outputs(_manifest()))["graphify-marker"] is True


def test_graphify_marker_is_the_september_pattern_and_passes_only_when_observed() -> None:
    assert GRAPHIFY_MARKER_PATTERN == r"AST extraction: 100/119 uncached files \(84%\)"
    outputs = _passing_outputs(_manifest())
    assert _checks(outputs, graphify_marker_pattern=GRAPHIFY_MARKER_PATTERN)["graphify-marker"]
    outputs["graphify-extract"] = _ok("extracted\n")
    assert not _checks(outputs, graphify_marker_pattern=GRAPHIFY_MARKER_PATTERN)["graphify-marker"]


@pytest.mark.parametrize(
    ("rows", "clean", "expected"),
    [(0, 9, False), (1, 9, True), (8, 9, True), (9, 9, False), (3, None, False), (None, 9, False)],
)
def test_c7_poller_marker_requires_rows_strictly_between_zero_and_the_clean_count(
    rows: int | None, clean: int | None, expected: bool
) -> None:
    assert c7_poller_marker_ok(rows, clean) is expected
