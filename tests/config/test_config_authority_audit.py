"""Repository-wide guardrails for the reactive configuration authority."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from gobby.runner_init.config_subscribers import live_consumer_matrix
from gobby.storage.config_store import ConfigStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = REPOSITORY_ROOT / "src" / "gobby"
CONTRACT = (
    REPOSITORY_ROOT / "crates" / "gcore" / "assets" / "config" / "runtime_config_contract.json"
)
GENERATOR = REPOSITORY_ROOT / "scripts" / "generate_runtime_config_contract.py"
AUDIT = REPOSITORY_ROOT / "docs" / "audits" / "configuration-audit.md"

UNREGISTERED_RUNTIME_CLAIM = re.compile(
    r"(?:"
    r"\b(?:not|never|no longer)\b.{0,64}\b(?:registered|registry)\b"
    r"|\b(?:unregistered|absent|excluded|missing|outside)\b.{0,64}"
    r"\b(?:registry|registered|runtime config(?:uration)?)\b"
    r"|\b(?:registry|registered)\b.{0,64}\b(?:does not|doesn't|not)\b"
    r")",
    re.IGNORECASE,
)

RAW_CONFIG_METHODS = frozenset(
    {
        "clear_secret",
        "delete",
        "get",
        "get_all",
        "get_secret_keys",
        "list_keys",
        "mark_secret_keys",
        "set",
        "set_many",
        "set_secret",
    }
)
AUDITED_RUNTIME_PATHS = (
    "src/gobby/runner_lifecycle.py",
    "src/gobby/runner_lifecycle_agents.py",
    "src/gobby/runner_lifecycle_periodic.py",
    "src/gobby/runner_lifecycle_subsystems.py",
    "src/gobby/runner_service_readiness.py",
    "src/gobby/servers/routes/attention.py",
    "src/gobby/servers/routes/configuration_generation_endpoints.py",
    "src/gobby/servers/routes/configuration_validation_detection.py",
    "src/gobby/servers/routes/sessions/core.py",
)


def _python_sources() -> list[Path]:
    return sorted(PYTHON_ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def _annotation_names(annotation: ast.expr | None) -> set[str]:
    if annotation is None:
        return set()
    return {node.id for node in ast.walk(annotation) if isinstance(node, ast.Name)}


def _assigned_names(target: ast.expr) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _store_names(tree: ast.AST) -> set[str]:
    names = {"config_store"}
    raw_protocols = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and "Config" in node.name
        and any("Protocol" in _annotation_names(base) for base in node.bases)
        and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name in RAW_CONFIG_METHODS
            for member in node.body
        )
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotation = node.annotation
            annotation_names = _annotation_names(annotation)
            if "ConfigStore" in annotation_names or annotation_names.intersection(raw_protocols):
                if isinstance(node, ast.arg):
                    names.add(node.arg)
                elif isinstance(node.target, ast.Name):
                    names.add(node.target.id)
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "ConfigStore"
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            names.update(_assigned_names(target))
    return names


def _is_raw_store_receiver(receiver: ast.expr, store_names: set[str]) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id in store_names
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "config_store"
    if (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "ConfigStore"
    ):
        return True
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Attribute)
        and receiver.func.attr == "get_config_store"
    )


def _raw_access_violations(path: Path, tree: ast.AST) -> list[str]:
    relative = _relative(path)
    store_names = _store_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in RAW_CONFIG_METHODS:
            continue
        if _is_raw_store_receiver(node.func.value, store_names):
            violations.append(f"{relative}:{node.lineno}: raw ConfigStore.{node.func.attr}")
    return violations


def _mutable_authority_violations(path: Path, tree: ast.AST) -> list[str]:
    relative = _relative(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in {"load_config", "load_full_config_from_db"}:
                    violations.append(f"{relative}:{node.lineno}: imports {alias.name}")
        if isinstance(node, ast.Attribute) and node.attr in {"config", "config_store"}:
            value = node.value
            if isinstance(value, ast.Name) and value.id == "runner":
                violations.append(f"{relative}:{node.lineno}: runner.{node.attr}")
            if (isinstance(value, ast.Name) and value.id == "services") or (
                isinstance(value, ast.Attribute) and value.attr == "services"
            ):
                violations.append(f"{relative}:{node.lineno}: ServiceContainer.{node.attr}")
        if isinstance(node, ast.ClassDef) and node.name in {"GobbyRunner", "ServiceContainer"}:
            for statement in node.body:
                target = statement.target if isinstance(statement, ast.AnnAssign) else None
                if isinstance(target, ast.Name) and target.id in {"config", "config_store"}:
                    violations.append(f"{relative}:{statement.lineno}: {node.name}.{target.id}")
    return violations


def test_python_runtime_has_one_config_authority() -> None:
    for relative in AUDITED_RUNTIME_PATHS:
        assert (REPOSITORY_ROOT / relative).is_file(), relative

    violations: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(), filename=path)
        violations.extend(_raw_access_violations(path, tree))
        violations.extend(_mutable_authority_violations(path, tree))

    assert violations == []


def test_cross_language_registry_coverage() -> None:
    generated = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    assert generated.returncode == 0, generated.stderr.decode()
    assert generated.stdout == CONTRACT.read_bytes()

    contract = json.loads(generated.stdout)
    exact_keys = [entry["key"] for entry in contract["exactKeys"]]
    patterns = [entry["pattern"] for entry in contract["patterns"]]
    assert len(exact_keys) == len(set(exact_keys))
    assert len(patterns) == len(set(patterns))
    assert live_consumer_matrix()

    rust_contract_tests = (
        REPOSITORY_ROOT / "crates" / "gcode" / "src" / "config" / "tests" / "runtime_contract.rs"
    ).read_text()
    assert "fn gobby_mode_uses_registry_authority()" in rust_contract_tests
    assert "fn standalone_mode_preserves_env_yaml_precedence()" not in rust_contract_tests

    browser_audit = (
        REPOSITORY_ROOT / "web" / "src" / "__tests__" / "config-authority-audit.test.ts"
    ).read_text()
    assert "web_has_one_config_authority" in browser_audit
    assert "authority_patches_carry_the_current_revision" in browser_audit


def test_audit_registration_claims_match_runtime_contract() -> None:
    contract = json.loads(CONTRACT.read_text())
    registered_keys = {entry["key"] for entry in contract["exactKeys"]}
    audit_rows: dict[str, list[list[str]]] = {}
    for line in AUDIT.read_text().splitlines():
        if not line.startswith("| "):
            continue
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) == 7:
            audit_rows.setdefault(columns[0], []).append(columns[1:])

    duplicate_rows = {key: rows for key, rows in audit_rows.items() if len(rows) > 1}
    assert duplicate_rows == {}
    unique_audit_rows = {key: rows[0] for key, rows in audit_rows.items()}

    registered_rows = registered_keys.intersection(unique_audit_rows)
    contradictory_rows = {
        key: unique_audit_rows[key][0]
        for key in registered_rows
        if UNREGISTERED_RUNTIME_CLAIM.search(unique_audit_rows[key][0])
    }
    assert "auth.username" not in registered_keys
    assert "auth.username" not in registered_rows
    assert unique_audit_rows["auth.username"][0].startswith("Retired by account-identity-cutover")
    assert contradictory_rows == {}


def test_unregistered_runtime_claim_detector_catches_reworded_claim() -> None:
    claim = "Auth service; outside the runtime configuration registry"

    assert UNREGISTERED_RUNTIME_CLAIM.search(claim)


def test_audit_status_legend_documents_cli_only_rows() -> None:
    assert "- `cli-only`:" in AUDIT.read_text()


def test_legacy_config_surfaces_are_absent() -> None:
    for method_name in RAW_CONFIG_METHODS:
        assert not hasattr(ConfigStore, method_name)

    app_tree = ast.parse((PYTHON_ROOT / "config" / "app.py").read_text())
    definitions = {node.name for node in ast.walk(app_tree) if isinstance(node, ast.FunctionDef)}
    assert "load_config" not in definitions
    assert "load_full_config_from_db" not in definitions

    mcp_source = (PYTHON_ROOT / "mcp_proxy" / "tools" / "config.py").read_text()
    for tool_name in ("get_config", "set_config", "update_config", "reset_config"):
        assert f'name="{tool_name}"' not in mcp_source

    guide = (REPOSITORY_ROOT / "docs" / "guides" / "configuration.md").read_text()
    assert "Reactive runtime configuration contract" in guide
