"""Phase 7 gobby-code FalkorDB migration contract tests."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _gobby_cli_repo() -> Path:
    candidates: list[Path] = []
    if env_path := os.environ.get("GOBBY_CLI_REPO"):
        candidates.append(Path(env_path))
    candidates.extend(
        [
            ROOT.parent / "gobby-cli",
            Path.home() / "Projects" / "gobby-cli",
        ]
    )

    for candidate in candidates:
        if (candidate / "crates/gcode/Cargo.toml").is_file():
            return candidate

    pytest.skip(
        "Phase 7 contract tests require a gobby-cli checkout. Set GOBBY_CLI_REPO to the repo root."
    )


def _read(relative_path: str) -> str:
    return (_gobby_cli_repo() / relative_path).read_text()


def _toml(relative_path: str) -> dict[str, Any]:
    return tomllib.loads(_read(relative_path))


def _struct_body(source: str, name: str) -> str:
    match = re.search(rf"pub\s+struct\s+{name}\s*\{{(?P<body>.*?)\n\}}", source, re.S)
    assert match is not None, f"missing public Rust struct {name}"
    return match.group("body")


def _without_rust_unit_tests(source: str) -> str:
    return source.split("#[cfg(test)]", maxsplit=1)[0]


def _assert_field(body: str, declaration: str) -> None:
    assert declaration in body, f"missing field declaration: {declaration}"


def test_phase7_config_tracks_falkordb_and_neo4j_cutover() -> None:
    """FalkorDB config is required; Neo4j config is transitional until 7.4."""
    config = _read("crates/gcode/src/config.rs")

    falkor = _struct_body(config, "FalkorConfig")
    _assert_field(falkor, "pub host: String")
    _assert_field(falkor, "pub port: u16")
    _assert_field(falkor, "pub password: Option<String>")
    _assert_field(falkor, "pub graph_name: String")

    context = _struct_body(config, "Context")
    _assert_field(context, "pub falkordb: Option<FalkorConfig>")

    assert re.search(r"let\s+falkordb\s*=\s*resolve_falkordb_config\(", config)
    graph_name_literal = re.search(r"graph_name:\s*\"gobby_code\"\.to_string\(\)", config)
    graph_name_const = 'const FALKORDB_GRAPH_NAME: &str = "gobby_code";' in config and re.search(
        r"graph_name:\s*FALKORDB_GRAPH_NAME\.to_string\(\)", config
    )
    assert graph_name_literal or graph_name_const
    assert "databases.falkordb.host" in config
    assert "databases.falkordb.port" in config
    assert "databases.falkordb.requirepass" in config
    assert "GOBBY_FALKORDB_HOST" in config
    assert "GOBBY_FALKORDB_PORT" in config
    assert "GOBBY_FALKORDB_PASSWORD" in config

    if re.search(r"pub\s+struct\s+Neo4jConfig\s*\{", config):
        neo4j = _struct_body(config, "Neo4jConfig")
        _assert_field(neo4j, "pub url: String")
        _assert_field(neo4j, "pub auth: Option<String>")
        _assert_field(neo4j, "pub database: String")
        _assert_field(context, "pub neo4j: Option<Neo4jConfig>")
        assert re.search(r"let\s+neo4j\s*=\s*resolve_neo4j_config\(", config)
    else:
        assert "pub neo4j: Option<Neo4jConfig>" not in context
        assert "resolve_neo4j_config" not in config


def test_phase7_falkor_client_pins_mutable_read_only_wrapper_contract() -> None:
    """Phase 7.2 pins the FalkorDB wrapper surface before query bodies are ported."""
    falkor = _read("crates/gcode/src/falkor.rs")

    client = _struct_body(falkor, "FalkorClient")
    _assert_field(client, "graph: SyncGraph")

    assert "use falkordb::" in falkor
    assert "FalkorClientBuilder" in falkor
    assert "FalkorConnectionInfo" in falkor
    assert "FalkorValue" in falkor
    assert "SyncGraph" in falkor
    assert re.search(r"pub\s+type\s+Row\s*=\s*HashMap<String,\s*Value>", falkor)
    assert re.search(r"pub\s+fn\s+from_config\(config:\s*&FalkorConfig\)", falkor)
    assert "urlencoding::encode(password)" in falkor
    assert "falkor://:{}@{}:{}" in falkor
    assert ".with_connection_info(conn_info)" in falkor
    assert re.search(
        r"pub\s+fn\s+query\(\s*&mut\s+self,\s*cypher:\s*&str,\s*"
        r"params:\s*Option<HashMap<String,\s*String>>",
        falkor,
        re.S,
    )
    assert ".with_params(&" in falkor
    assert re.search(r"fn\s+parse_falkor_result\(", falkor)
    assert "result.header" in falkor
    assert "FalkorValue::None" in falkor
    assert re.search(
        r"pub\s+fn\s+with_falkor<T>\(\s*ctx:\s*&Context,\s*default:\s*T,\s*"
        r"f:\s*impl\s+FnOnce\(&mut\s+FalkorClient\)",
        falkor,
        re.S,
    )
    assert re.search(r"let\s+mut\s+client\s*=", falkor)
    assert "ctx.falkordb" in falkor


def test_phase7_cargo_dependencies_and_lockfile_track_falkordb_client() -> None:
    """Phase 7.2 adds the Rust FalkorDB deps and keeps the lockfile reproducible."""
    cargo = _toml("crates/gcode/Cargo.toml")
    lockfile = _toml("Cargo.lock")

    assert cargo["package"]["name"] == "gobby-code"
    assert {"name": "gcode", "path": "src/main.rs"} in cargo["bin"]

    dependencies = cargo["dependencies"]
    assert dependencies["falkordb"] == "0.2"
    assert dependencies["urlencoding"] == "2"
    assert "base64" in dependencies
    assert "reqwest" in dependencies

    package_names = {package["name"] for package in lockfile["package"]}
    assert "falkordb" in package_names
    assert "urlencoding" in package_names
    assert "neo4j" not in package_names
    assert "neo4rs" not in package_names


def test_phase7_ports_all_eight_read_queries_without_unbound_numeric_params() -> None:
    """Phase 7.3 ports every Rust graph read helper to FalkorDB query semantics."""
    falkor = _read("crates/gcode/src/falkor.rs")
    production = _without_rust_unit_tests(falkor)

    for function in (
        "count_callers",
        "count_usages",
        "find_callers",
        "find_usages",
        "find_callers_batch",
        "find_callees_batch",
        "get_imports",
        "blast_radius",
    ):
        assert re.search(
            rf"pub\s+fn\s+{function}\(\s*ctx:\s*&Context\b",
            production,
        ), f"missing public read helper {function}(ctx: &Context, ...)"

    assert "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol" in production
    assert re.search(r"fn\s+cypher_string_literal\(s:\s*&str\)\s*->\s*String", production)
    assert re.search(r"fn\s+id_list_literal\(ids:\s*&\[String\]\)\s*->\s*String", production)
    assert re.search(
        r"fn\s+blast_radius_query\(depth:\s*usize,\s*limit:\s*usize\)",
        production,
    )
    assert "depth.clamp(1, 5)" in production
    assert "limit.clamp(1, MAX_GRAPH_LIMIT)" in production
    assert "SKIP {offset} LIMIT {limit}" in production
    assert "target.id IN [{ids}]" in production
    assert "src.id IN [{ids}]" in production
    assert "LIMIT {limit}" in production
    assert "$offset" not in production
    assert "$limit" not in production
    assert "$ids" not in production
