"""C0 runs the matrix's compatibility commands against one frozen manifest and fails closed on each pin."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prepare_daemon import isolated_environment
from runtime_boundary import require, write_once

GRAPHIFY_PIN = "0.9.103"
GCODE_PIN = "1.9.104"
# September's interrupt marker: run_c7.py MARKER_PATTERN and command-manifest.json c7_markers.
GRAPHIFY_MARKER_PATTERN = r"AST extraction: 100/119 uncached files \(84%\)"
GCODE_CONTRACT_VERSION = 12
CALLERS_LIMIT = 20
COMMAND_TIMEOUT_SECONDS = 1800
PROBE_SYMBOLS = (
    "load_policy",
    "repository_root",
    "load_runtime_settings",
    "load_database_settings",
    "load_slack_settings",
)
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


def select_native_concurrency(observed_limits: Sequence[int]) -> int:
    """Choose the conservative native fan-out: the lowest positive limit the probe recorded."""
    require(len(observed_limits) > 0, "native concurrency probe recorded no limit")
    for limit in observed_limits:
        require(
            isinstance(limit, int) and not isinstance(limit, bool) and limit > 0,
            f"native concurrency limit is not a positive integer: {limit!r}",
        )
    return min(observed_limits)


def build_command_manifest(
    root: Path, *, probe_symbols: tuple[str, ...], graphify_concurrency: int
) -> dict[str, Any]:
    require(
        isinstance(graphify_concurrency, int)
        and not isinstance(graphify_concurrency, bool)
        and graphify_concurrency > 0,
        "GRAPHIFY_NATIVE_CONCURRENCY must be a positive integer",
    )
    graphify = str(root / "tools/graphify/venv/bin/graphify")
    gcode = str(root / "tools/gcode/bin/gcode")
    graphify_corpus = root / "corpora/graphify/C0"
    gcode_corpus = root / "corpora/gcode/C0"
    graphify_env = {"GRAPHIFY_NATIVE_CONCURRENCY": str(graphify_concurrency)}
    commands: list[dict[str, Any]] = [
        _command("graphify-version", [graphify, "--version"], graphify_corpus, graphify_env),
        _command("graphify-help", [graphify, "--help"], graphify_corpus, graphify_env),
        _command(
            "graphify-export-help", [graphify, "export", "--help"], graphify_corpus, graphify_env
        ),
        _command(
            "graphify-extract",
            [
                graphify,
                "extract",
                str(graphify_corpus),
                "--code-only",
                "--no-cluster",
                "--out",
                str(root / "c0/graphify-state"),
            ],
            graphify_corpus,
            graphify_env,
        ),
        _command("gcode-version", [gcode, "--version"], gcode_corpus, {}),
        _command("gcode-contract", [gcode, "--format", "json", "contract"], gcode_corpus, {}),
        _command("gcode-schema-identity", [gcode, "schema-identity", "--json"], gcode_corpus, {}),
        _command(
            "gcode-embeddings-doctor",
            [gcode, "--format", "json", "embeddings", "doctor"],
            gcode_corpus,
            {},
        ),
        _command("gcode-status", [gcode, "--format", "json", "status"], gcode_corpus, {}),
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
                {},
            )
        )
    return {
        "pins": {
            "graphify": GRAPHIFY_PIN,
            "gcode": GCODE_PIN,
            "gcode_contract": GCODE_CONTRACT_VERSION,
        },
        "probe_symbols": list(probe_symbols),
        "callers_limit": CALLERS_LIMIT,
        "graphify_concurrency": graphify_concurrency,
        "commands": commands,
    }


def _command(name: str, argv: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    return {"name": name, "argv": argv, "cwd": str(cwd), "env": env}


def freeze_command_manifest(
    root: Path, *, probe_symbols: tuple[str, ...], graphify_concurrency: int
) -> str:
    text = (
        json.dumps(
            build_command_manifest(
                root, probe_symbols=probe_symbols, graphify_concurrency=graphify_concurrency
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


def require_probe_symbols(corpus: Path, symbols: Sequence[str]) -> None:
    """Fail closed before any state query when a ruled symbol is absent from the pinned corpus."""
    require(len(symbols) > 0, "probe symbols are empty")
    sources = [path.read_text(encoding="utf-8") for path in sorted(corpus.rglob("*.py"))]
    missing = [
        symbol
        for symbol in symbols
        if not any(
            re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b", text, re.M)
            for text in sources
        )
    ]
    require(not missing, f"probe symbols missing from pinned corpus {corpus}: {missing}")


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


def run_c0(
    root: Path,
    attempt: str,
    runner: Runner,
    *,
    ambient: dict[str, str],
    expected_schema_identity: Any,
    c7_poller_rows: int | None,
    c7_clean_count: int | None,
    graphify_marker_pattern: str | None,
) -> dict[str, Any]:
    require(ATTEMPT_PATTERN.fullmatch(attempt) is not None, f"invalid attempt: {attempt!r}")
    manifest_path = root / MANIFEST_RELATIVE
    require(manifest_path.is_file(), "command manifest is not frozen")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require_probe_symbols(root / "corpora/graphify/C0", manifest["probe_symbols"])
    write_once(
        root / f"receipts/c0-symbols-{attempt}.json",
        json.dumps({"probe_symbols": manifest["probe_symbols"]}, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    log_dir = root / "logs/c0" / attempt
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_env = isolated_environment(root, ambient)
    outputs: dict[str, CommandResult] = {}
    results: list[dict[str, Any]] = []
    for command in manifest["commands"]:
        child = {**base_env, **command["env"]}
        result = runner(command["argv"], Path(command["cwd"]), child)
        outputs[command["name"]] = result
        write_once(log_dir / f"{command['name']}.stdout", result.stdout, mode=0o600)
        write_once(log_dir / f"{command['name']}.stderr", result.stderr, mode=0o600)
        results.append(
            {
                "name": command["name"],
                "argv": command["argv"],
                "exit_code": result.returncode,
                "env_diff": _env_diff(ambient, child),
            }
        )
    checks = evaluate_c0(
        outputs,
        manifest,
        expected_schema_identity=expected_schema_identity,
        c7_poller_rows=c7_poller_rows,
        c7_clean_count=c7_clean_count,
        graphify_marker_pattern=graphify_marker_pattern,
    )
    receipt = {
        "attempt": attempt,
        "pins": manifest["pins"],
        "probe_symbols": manifest["probe_symbols"],
        "graphify_concurrency": manifest["graphify_concurrency"],
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
    if help_result is None or export_result is None or help_result.returncode != 0:
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
    expected_schema_identity: Any,
    c7_poller_rows: int | None,
    c7_clean_count: int | None,
    graphify_marker_pattern: str | None,
) -> list[dict[str, Any]]:
    contract = _payload(outputs.get("gcode-contract"))
    identity = _payload(outputs.get("gcode-schema-identity"))
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
        and re.search(graphify_marker_pattern, extract.stdout + extract.stderr) is not None
    )
    concurrency = manifest.get("graphify_concurrency")
    checks = {
        "graphify-pin": _pin_line(outputs.get("graphify-version"), f"graphify {GRAPHIFY_PIN}"),
        "gcode-pin": _pin_line(outputs.get("gcode-version"), f"gcode {GCODE_PIN}"),
        "gcode-contract": isinstance(contract, dict)
        and contract.get("tool") == "gcode"
        and contract.get("contract_version") == GCODE_CONTRACT_VERSION,
        "graphify-inventory": _inventory_ok(
            outputs.get("graphify-help"), outputs.get("graphify-export-help")
        ),
        "gcode-schema-identity": expected_schema_identity is not None
        and identity == expected_schema_identity,
        "embedding-preflight": doctor is not None
        and doctor.returncode == 0
        and isinstance(doctor_payload, dict)
        and doctor_payload.get("agrees") is True,
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
        "graphify-concurrency": isinstance(concurrency, int)
        and not isinstance(concurrency, bool)
        and concurrency > 0,
    }
    return [{"check": name, "passed": bool(passed)} for name, passed in checks.items()]


def c7_poller_marker_ok(rows: int | None, clean: int | None) -> bool:
    """The C7 marker is a cold-index row count strictly between zero and the clean file count."""
    return rows is not None and clean is not None and 0 < rows < clean
