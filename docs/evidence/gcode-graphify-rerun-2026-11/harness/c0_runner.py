"""C0 runs the matrix's compatibility commands against one frozen manifest and fails closed on each pin."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c0_poller import IndexPoller
from prepare_daemon import isolated_environment
from provision_environment import C0_WORKSPACE
from runtime_boundary import EvidenceError, require, write_once
from schema_head import gcode_binary_relative, read_archived_gcode_version

GRAPHIFY_PIN = "0.9.103"
# September's interrupt marker: run_c7.py MARKER_PATTERN and command-manifest.json c7_markers.
GRAPHIFY_MARKER_PATTERN = r"AST extraction: 100/119 uncached files \(84%\)"
GCODE_CONTRACT_VERSION = 12
CALLERS_LIMIT = 20
COMMAND_TIMEOUT_SECONDS = 1800
PROBE_SYMBOLS = (
    "load_policy",
    "repository_root",
    "plan_product",
    "MCPError",
    "SettingsError",
)
PROBE_MIN_FILES = 3
COLD_INDEX_NAME = "gcode-index-cold"
GRAPHIFY_CONCURRENCY_FLAG = "--max-workers"
MAX_WORKERS_LINE = re.compile(r"(?<![\w-])--max-workers(?![\w-])")
# September's C0 extract record: its redacted environment carries GRAPHIFY_NATIVE_CONCURRENCY.
SEPTEMBER_RUN_RECORDS = "results/index-comparison/run-records.jsonl"
SEPTEMBER_C0_EXTRACT_LINE = 3
GRAPHIFY_BINARY = "tools/graphify/venv/bin/graphify"
GRAPHIFY_CORPUS = "corpora/graphify/C0"
GRAPHIFY_INVENTORY = (
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
MANIFEST_RELATIVE = "c0/command-manifest.json"
ATTEMPT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


Runner = Callable[[list[str], Path, dict[str, str]], CommandResult]


def september_max_workers(september_root: Path) -> tuple[int, str]:
    """September's C0 extract record carries the concurrency value; that record is the source."""
    record_path = september_root / SEPTEMBER_RUN_RECORDS
    lines = record_path.read_text(encoding="utf-8").splitlines()
    require(len(lines) >= SEPTEMBER_C0_EXTRACT_LINE, f"{record_path} has no C0 extract record")
    record = json.loads(lines[SEPTEMBER_C0_EXTRACT_LINE - 1])
    recorded = record.get("redacted_environment_diff", {}).get("GRAPHIFY_NATIVE_CONCURRENCY")
    require(
        isinstance(recorded, str) and recorded.isdigit() and int(recorded) > 0,
        f"{record_path}:{SEPTEMBER_C0_EXTRACT_LINE} records no positive GRAPHIFY_NATIVE_CONCURRENCY",
    )
    argv: list[str] = record.get("command_argv", [])
    flag_at = argv.index(GRAPHIFY_CONCURRENCY_FLAG) if GRAPHIFY_CONCURRENCY_FLAG in argv else -1
    require(
        0 <= flag_at < len(argv) - 1 and argv[flag_at + 1] == recorded,
        f"{record_path}:{SEPTEMBER_C0_EXTRACT_LINE} argv disagrees with its recorded concurrency",
    )
    return int(recorded), f"{record_path}:{SEPTEMBER_C0_EXTRACT_LINE}"


def decide_graphify_concurrency(
    top_help: CommandResult,
    top_path: Path,
    extract_help: CommandResult,
    help_path: Path,
    september_value: int,
    september_evidence: str,
) -> dict[str, Any]:
    """Freeze September's --max-workers when this release's top-level or extract help lists it."""
    require(top_help.returncode == 0, "graphify --help probe failed")
    require(extract_help.returncode == 0, "graphify extract --help probe failed")
    for path, result in ((top_path, top_help), (help_path, extract_help)):
        for number, line in enumerate(result.stdout.splitlines(), start=1):
            if MAX_WORKERS_LINE.search(line):
                return {
                    "value": september_value,
                    "source": "september",
                    "flag": GRAPHIFY_CONCURRENCY_FLAG,
                    "evidence": [f"{path}:{number}", september_evidence],
                }
    return {"value": None, "source": "unsupported", "flag": None, "evidence": [str(help_path)]}


def probe_graphify_concurrency(
    root: Path,
    attempt: str,
    runner: Runner,
    *,
    september_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """Capture this release's graphify help first, then decide the concurrency receipt from it."""
    graphify = str(root / GRAPHIFY_BINARY)
    corpus = root / GRAPHIFY_CORPUS
    probe_dir = root / "logs/c0" / attempt / "probe"
    probe_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    top_help = runner([graphify, "--help"], corpus, env)
    extract_help = runner([graphify, "extract", "--help"], corpus, env)
    top_path = probe_dir / "graphify-help.stdout"
    extract_path = probe_dir / "graphify-extract-help.stdout"
    write_once(top_path, top_help.stdout, mode=0o600)
    write_once(extract_path, extract_help.stdout, mode=0o600)
    write_once(probe_dir / "graphify-help.stderr", top_help.stderr, mode=0o600)
    write_once(probe_dir / "graphify-extract-help.stderr", extract_help.stderr, mode=0o600)
    returns = {
        "graphify-help": {"returncode": top_help.returncode, "stderr": top_help.stderr},
        "graphify-extract-help": {
            "returncode": extract_help.returncode,
            "stderr": extract_help.stderr,
        },
    }
    write_once(probe_dir / "graphify-probes.json", json.dumps(returns, indent=2), mode=0o600)
    value, evidence = september_max_workers(september_root)
    return decide_graphify_concurrency(
        top_help, top_path, extract_help, extract_path, value, evidence
    )


def concurrency_receipt_ok(receipt: object) -> bool:
    if not isinstance(receipt, dict):
        return False
    value = receipt.get("value")
    if value is None:
        return receipt.get("source") == "unsupported" and receipt.get("flag") is None
    evidence = receipt.get("evidence")
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        and receipt.get("source") == "september"
        and receipt.get("flag") == GRAPHIFY_CONCURRENCY_FLAG
        and isinstance(evidence, list)
        and len(evidence) == 2
    )


def build_command_manifest(
    root: Path,
    *,
    probe_symbols: tuple[str, ...],
    graphify_concurrency: Mapping[str, Any],
    gcode_version: str,
) -> dict[str, Any]:
    require(
        concurrency_receipt_ok(graphify_concurrency),
        "graphify concurrency receipt is not the recorded shape",
    )
    graphify = str(root / GRAPHIFY_BINARY)
    gcode = str(root / gcode_binary_relative())
    graphify_corpus = root / GRAPHIFY_CORPUS
    gcode_corpus = root / C0_WORKSPACE
    extract = [
        graphify,
        "extract",
        str(graphify_corpus),
        "--code-only",
        "--no-cluster",
        "--out",
        str(root / "c0/graphify-state"),
    ]
    if graphify_concurrency["value"] is not None:
        extract += [GRAPHIFY_CONCURRENCY_FLAG, str(graphify_concurrency["value"])]
    commands: list[dict[str, Any]] = [
        _command("graphify-version", [graphify, "--version"], graphify_corpus),
        _command("graphify-help", [graphify, "--help"], graphify_corpus),
        _command("graphify-export-help", [graphify, "export", "--help"], graphify_corpus),
        _command("graphify-extract", extract, graphify_corpus),
        _command(
            COLD_INDEX_NAME,
            [
                gcode,
                "--project",
                str(gcode_corpus),
                "--format",
                "json",
                "index",
                str(gcode_corpus),
                "--full",
                "--sync-projections",
            ],
            gcode_corpus,
        ),
        _command("gcode-version", [gcode, "--version"], gcode_corpus),
        _command("gcode-contract", [gcode, "--format", "json", "contract"], gcode_corpus),
        _command("gcode-schema-identity", [gcode, "schema-identity", "--json"], gcode_corpus),
        _command(
            "gcode-embeddings-doctor",
            [gcode, "--format", "json", "embeddings", "doctor"],
            gcode_corpus,
        ),
        _command("gcode-status", [gcode, "--format", "json", "status"], gcode_corpus),
    ]
    for index, symbol in enumerate(probe_symbols, start=1):
        commands.append(
            _command(
                f"gcode-callers-{index}",
                [
                    gcode,
                    "--format",
                    "json",
                    "callers",
                    symbol,
                    "--limit",
                    str(CALLERS_LIMIT),
                ],
                gcode_corpus,
            )
        )
    return {
        "pins": {
            "graphify": GRAPHIFY_PIN,
            "gcode": gcode_version,
            "gcode_contract": GCODE_CONTRACT_VERSION,
        },
        "probe_symbols": list(probe_symbols),
        "callers_limit": CALLERS_LIMIT,
        "graphify_concurrency": dict(graphify_concurrency),
        "commands": commands,
    }


def _command(name: str, argv: list[str], cwd: Path) -> dict[str, Any]:
    return {"name": name, "argv": argv, "cwd": str(cwd)}


def freeze_command_manifest(
    root: Path, *, probe_symbols: tuple[str, ...], graphify_concurrency: Mapping[str, Any]
) -> str:
    text = (
        json.dumps(
            build_command_manifest(
                root,
                probe_symbols=probe_symbols,
                graphify_concurrency=graphify_concurrency,
                gcode_version=read_archived_gcode_version(root),
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    path = root / MANIFEST_RELATIVE
    if path.exists():
        require(
            path.read_text(encoding="utf-8") == text,
            "command manifest changed: refusing to refreeze",
        )
        return text
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_once(path, text, mode=0o444)
    return text


def _definition_files(corpus: Path, symbols: Sequence[str]) -> dict[str, set[str]]:
    sources = {path: path.read_text(encoding="utf-8") for path in sorted(corpus.rglob("*.py"))}
    return {
        symbol: {
            str(path.relative_to(corpus))
            for path, text in sources.items()
            if re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b", text, re.M)
        }
        for symbol in symbols
    }


def require_probe_symbols(corpus: Path, symbols: Sequence[str]) -> None:
    """Fail closed before any state query when a ruled symbol has no definition in the corpus."""
    require(len(symbols) > 0, "probe symbols are empty")
    definitions = _definition_files(corpus, symbols)
    missing = [symbol for symbol in symbols if not definitions[symbol]]
    require(not missing, f"probe symbols missing from pinned corpus {corpus}: {missing}")


def require_probe_spread(corpus: Path, symbols: Sequence[str]) -> set[str]:
    """The probe reaches at least PROBE_MIN_FILES files, so one module cannot stand for the tree."""
    files: set[str] = set().union(*_definition_files(corpus, symbols).values())
    require(
        len(files) >= PROBE_MIN_FILES,
        f"probe symbols span {len(files)} files; need at least {PROBE_MIN_FILES}",
    )
    return files


def run_process(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
    """Run one frozen argv with the isolated environment; a timeout is a failed command."""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(stdout="", stderr="timeout", returncode=124)
    return CommandResult(
        stdout=completed.stdout, stderr=completed.stderr, returncode=completed.returncode
    )


def _env_diff(ambient: Mapping[str, str], child: Mapping[str, str]) -> dict[str, list[str]]:
    # Names only: a value never enters the receipt.
    return {
        "removed": sorted(set(ambient) - set(child)),
        "added": sorted(set(child) - set(ambient)),
        "changed": sorted(
            name for name in set(ambient) & set(child) if ambient[name] != child[name]
        ),
    }


def _stop_watch(watch: IndexPoller | None) -> EvidenceError | None:
    """Stops the poller and returns its failure, so the caller writes the command's logs first."""
    if watch is None:
        return None
    try:
        watch.stop()
    except EvidenceError as error:
        return error
    return None


def run_c0(
    root: Path,
    attempt: str,
    runner: Runner,
    *,
    ambient: dict[str, str],
    project_id: str,
    observer: IndexPoller | None,
    graphify_marker_pattern: str | None,
) -> dict[str, Any]:
    require(ATTEMPT_PATTERN.fullmatch(attempt) is not None, f"invalid attempt: {attempt!r}")
    manifest_path = root / MANIFEST_RELATIVE
    require(manifest_path.is_file(), "command manifest is not frozen")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require_probe_symbols(root / GRAPHIFY_CORPUS, manifest["probe_symbols"])
    require_probe_spread(root / GRAPHIFY_CORPUS, manifest["probe_symbols"])
    write_once(
        root / f"receipts/c0-symbols-{attempt}.json",
        json.dumps({"probe_symbols": manifest["probe_symbols"]}, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    log_dir = root / "logs/c0" / attempt
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    child = isolated_environment(root, ambient)
    outputs: dict[str, CommandResult] = {}
    results: list[dict[str, Any]] = []
    for command in manifest["commands"]:
        watch = observer if command["name"] == COLD_INDEX_NAME else None
        if watch is not None:
            watch.start()
        try:
            result = runner(command["argv"], Path(command["cwd"]), child)
        except BaseException:
            if watch is not None:
                watch.stop()
            raise
        poller_failure = _stop_watch(watch)
        outputs[command["name"]] = result
        write_once(log_dir / f"{command['name']}.stdout", result.stdout, mode=0o600)
        write_once(log_dir / f"{command['name']}.stderr", result.stderr, mode=0o600)
        if poller_failure is not None:
            # The command's logs are on disk first, so a failed poller still leaves the evidence.
            raise poller_failure
        if command["name"] == COLD_INDEX_NAME and result.returncode != 0:
            # The cold index is required: its failure stops C0 here, with its stderr kept above.
            raise EvidenceError(f"required cold index exited {result.returncode}: {result.stderr}")
        results.append(
            {
                "name": command["name"],
                "argv": command["argv"],
                "exit_code": result.returncode,
                "env_diff": _env_diff(ambient, child),
            }
        )
    first_qualifying = observer.first_qualifying if observer is not None else None
    clean = observer.clean if observer is not None else None
    checks = evaluate_c0(
        outputs,
        manifest,
        project_id=project_id,
        c7_poller_rows=first_qualifying,
        c7_clean_count=clean,
        graphify_marker_pattern=graphify_marker_pattern,
    )
    receipt = {
        "attempt": attempt,
        "pins": manifest["pins"],
        "probe_symbols": manifest["probe_symbols"],
        "graphify_concurrency": manifest["graphify_concurrency"],
        "c7": {"first_qualifying": first_qualifying, "clean": clean},
        "results": results,
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }
    write_once(
        root / f"receipts/c0-{attempt}.json",
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    return receipt


def _payload(result: CommandResult | None) -> Any:
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _pin_line(result: CommandResult | None, expected: str) -> bool:
    return result is not None and result.returncode == 0 and result.stdout.strip() == expected


def _inventory_ok(help_result: CommandResult | None, export_result: CommandResult | None) -> bool:
    if help_result is None or export_result is None:
        return False
    if help_result.returncode != 0 or export_result.returncode != 0:
        return False
    names = set(re.findall(r"^\s+([a-z][a-z-]*)", help_result.stdout, re.M))
    return (
        set(GRAPHIFY_INVENTORY) <= names
        and re.search(r"\bcallflow-html\b", export_result.stdout) is not None
    )


def evaluate_c0(
    outputs: dict[str, CommandResult],
    manifest: dict[str, Any],
    *,
    project_id: str,
    c7_poller_rows: int | None,
    c7_clean_count: int | None,
    graphify_marker_pattern: str | None,
) -> list[dict[str, Any]]:
    contract = _payload(outputs.get("gcode-contract"))
    doctor = outputs.get("gcode-embeddings-doctor")
    doctor_payload = _payload(doctor)
    status = _payload(outputs.get("gcode-status"))
    caller_names = [
        command["name"]
        for command in manifest["commands"]
        if command["name"].startswith("gcode-callers-")
    ]
    callers = [_payload(outputs.get(name)) for name in caller_names]
    extract = outputs.get("graphify-extract")
    marker_seen = (
        graphify_marker_pattern is not None
        and extract is not None
        # A marker printed before a failing exit is not evidence: the extract must exit 0.
        and extract.returncode == 0
        and re.search(graphify_marker_pattern, extract.stdout + extract.stderr) is not None
    )
    checks = {
        "graphify-pin": _pin_line(outputs.get("graphify-version"), f"graphify {GRAPHIFY_PIN}"),
        "gcode-pin": _pin_line(outputs.get("gcode-version"), f"gcode {manifest['pins']['gcode']}"),
        "gcode-contract": isinstance(contract, dict)
        and contract.get("tool") == "gcode"
        and contract.get("contract_version") == GCODE_CONTRACT_VERSION,
        "graphify-inventory": _inventory_ok(
            outputs.get("graphify-help"), outputs.get("graphify-export-help")
        ),
        "embedding-preflight": doctor is not None
        and doctor.returncode == 0
        and isinstance(doctor_payload, dict)
        and doctor_payload.get("agrees") is True,
        "gcode-status-project-id": isinstance(status, dict) and status.get("id") == project_id,
        "gcode-status-indexed": isinstance(status, dict)
        and "id" in status
        and isinstance(status.get("code_index"), dict)
        and status["code_index"].get("healthy") is True,
        "probe-set": len(manifest["probe_symbols"]) > 0
        and len(caller_names) == len(manifest["probe_symbols"])
        and all(name in outputs for name in caller_names),
        "callers-probe": len(callers) > 0
        and all(isinstance(item, dict) and item.get("total", 0) >= 1 for item in callers),
        "graphify-marker": marker_seen,
        "c7-poller-marker": c7_poller_marker_ok(c7_poller_rows, c7_clean_count),
        "graphify-concurrency": concurrency_receipt_ok(manifest.get("graphify_concurrency")),
    }
    return [{"check": name, "passed": bool(passed)} for name, passed in checks.items()]


def c7_poller_marker_ok(rows: int | None, clean: int | None) -> bool:
    """The C7 marker is a cold-index row count strictly between zero and the clean file count."""
    return rows is not None and clean is not None and 0 < rows < clean
